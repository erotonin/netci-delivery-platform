package homesync

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// Ownership is a job's lease: who may start its builds, under which epoch, for how long.
type Ownership struct {
	Job       string `json:"job"`
	Owner     string `json:"owner"`
	Epoch     int64  `json:"epoch"`
	ExpiresIn int64  `json:"expiresInMs"`
}

// OwnedError: another replica holds the job's live lease.
type OwnedError struct{ Ownership Ownership }

func (e *OwnedError) Error() string {
	return fmt.Sprintf("job %s is owned by %s", e.Ownership.Job, e.Ownership.Owner)
}

// ErrNotOwner: the caller does not hold the job's live lease under the epoch it gave.
var ErrNotOwner = errors.New("the job's lease is not held under this epoch")

// RunTakenError: the run has a number already, this one (ADR-081).
type RunTakenError struct {
	Job    string
	Number int
}

func (e *RunTakenError) Error() string {
	return fmt.Sprintf("the run has %s #%d already", e.Job, e.Number)
}

// RunReservation is where a run was given its number: by which replica, and when (the database's clock).
type RunReservation struct {
	Job        string
	Number     int
	Replica    string
	ReservedAt time.Time
}

// NumberTakenError: the number was reserved already; Max is the highest reservation.
type NumberTakenError struct{ Max int }

func (e *NumberTakenError) Error() string {
	return fmt.Sprintf("build numbers up to %d are reserved", e.Max)
}

// ClaimJob takes a job with no live owner, or renews the caller's own lease. A lease that expired
// and is taken again -- by anyone, the old owner included -- gets a new epoch: whatever was done
// under the old one cannot be continued under it.
func (s *Store) ClaimJob(ctx context.Context, job, replica string, ttl time.Duration) (Ownership, error) {
	if job == "" || replica == "" || ttl <= 0 || ttl > 5*time.Minute {
		return Ownership{}, errors.New("a job, a replica and a lease of at most 5 minutes are required")
	}
	var o Ownership
	err := s.Pool.QueryRow(ctx, `INSERT INTO home_job_owners (job, owner, epoch, expires_at)
		VALUES ($1, $2, nextval('home_owner_epochs'), now() + $3 * interval '1 millisecond')
		ON CONFLICT (job) DO UPDATE SET
			owner = EXCLUDED.owner,
			epoch = CASE WHEN home_job_owners.owner = EXCLUDED.owner AND home_job_owners.expires_at > now()
			             THEN home_job_owners.epoch ELSE EXCLUDED.epoch END,
			expires_at = EXCLUDED.expires_at
		WHERE home_job_owners.expires_at <= now() OR home_job_owners.owner = EXCLUDED.owner
		RETURNING job, owner, epoch, (extract(epoch FROM expires_at - now()) * 1000)::bigint`,
		job, replica, ttl.Milliseconds()).Scan(&o.Job, &o.Owner, &o.Epoch, &o.ExpiresIn)
	if errors.Is(err, pgx.ErrNoRows) {
		current, cerr := s.JobOwner(ctx, job)
		if cerr != nil {
			return Ownership{}, cerr
		}
		return Ownership{}, &OwnedError{Ownership: current}
	}
	return o, err
}

// JobOwner returns a job's lease as it stands (ExpiresIn <= 0: nobody holds it live).
func (s *Store) JobOwner(ctx context.Context, job string) (Ownership, error) {
	o := Ownership{Job: job}
	err := s.Pool.QueryRow(ctx, `SELECT owner, epoch, (extract(epoch FROM expires_at - now()) * 1000)::bigint
		FROM home_job_owners WHERE job = $1`, job).Scan(&o.Owner, &o.Epoch, &o.ExpiresIn)
	if errors.Is(err, pgx.ErrNoRows) {
		return o, nil
	}
	return o, err
}

// Reserve records that the job's next build has this number. It is refused unless the caller
// holds the job's live lease under epoch, and unless number is above every reservation so far;
// then the highest reservation comes back, for the caller to start from.
func (s *Store) Reserve(ctx context.Context, job string, number int, replica string, epoch int64) error {
	return s.ReserveRun(ctx, job, number, replica, epoch, "")
}

// ReserveRun is Reserve for a netCI run (runID, a UUID; "" for a build no run asked for): refused
// with a RunTakenError when the run has a number already, here or on another replica.
func (s *Store) ReserveRun(ctx context.Context, job string, number int, replica string, epoch int64, runID string) error {
	tx, err := s.Pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background()) //nolint:errcheck
	// The lease row locked: a takeover waits for this reservation, or this one sees the takeover.
	var live bool
	err = tx.QueryRow(ctx, `SELECT owner = $2 AND epoch = $3 AND expires_at > now() FROM home_job_owners
		WHERE job = $1 FOR UPDATE`, job, replica, epoch).Scan(&live)
	if errors.Is(err, pgx.ErrNoRows) || (err == nil && !live) {
		return ErrNotOwner
	}
	if err != nil {
		return err
	}
	var max int
	if err := tx.QueryRow(ctx, `SELECT COALESCE(max(number), 0) FROM home_build_numbers WHERE job = $1`, job).Scan(&max); err != nil {
		return err
	}
	if number <= max {
		return &NumberTakenError{Max: max}
	}
	var run *string
	if runID != "" {
		run = &runID
		if had, ok, err := runIn(ctx, tx, runID); err != nil {
			return err
		} else if ok {
			return &RunTakenError{Job: had.Job, Number: had.Number}
		}
	}
	if _, err := tx.Exec(ctx, `INSERT INTO home_build_numbers (job, number, owner, epoch, run_id) VALUES ($1, $2, $3, $4, $5)`,
		job, number, replica, epoch, run); err != nil {
		// Two replicas reserving the same run at once (two jobs' leases, a run moved between
		// jobs): the unique index decides, and the loser is told where the run went.
		var pgErr *pgconn.PgError
		if runID != "" && errors.As(err, &pgErr) && pgErr.Code == "23505" && pgErr.ConstraintName == "home_build_numbers_run" {
			_ = tx.Rollback(ctx)
			had, ok, rerr := s.Run(ctx, runID)
			if rerr == nil && ok {
				return &RunTakenError{Job: had.Job, Number: had.Number}
			}
		}
		return err
	}
	return tx.Commit(ctx)
}

type querier interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

func runIn(ctx context.Context, q querier, runID string) (RunReservation, bool, error) {
	var r RunReservation
	err := q.QueryRow(ctx, `SELECT job, number, owner, reserved_at FROM home_build_numbers WHERE run_id = $1`, runID).
		Scan(&r.Job, &r.Number, &r.Replica, &r.ReservedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return RunReservation{}, false, nil
	}
	return r, err == nil, err
}

// Run is the reservation a run was given, if any.
func (s *Store) Run(ctx context.Context, runID string) (RunReservation, bool, error) {
	return runIn(ctx, s.Pool, runID)
}

// ReleaseRun forgets that the run has job #number, so it may be given a number again: for a run
// whose number was reserved by a replica that died before the build existed. The number itself
// stays taken (numbers are never reused). False when the run is not, or no longer, there.
func (s *Store) ReleaseRun(ctx context.Context, runID, job string, number int) (bool, error) {
	tag, err := s.Pool.Exec(ctx, `UPDATE home_build_numbers SET run_id = NULL WHERE run_id = $1 AND job = $2 AND number = $3`,
		runID, job, number)
	return tag.RowsAffected() == 1, err
}

// MaxReserved is the highest number reserved for a job (0 for none): a new owner starts above it.
func (s *Store) MaxReserved(ctx context.Context, job string) (int, error) {
	var max int
	err := s.Pool.QueryRow(ctx, `SELECT COALESCE(max(number), 0) FROM home_build_numbers WHERE job = $1`, job).Scan(&max)
	return max, err
}
