package homesync

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
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
	if _, err := tx.Exec(ctx, `INSERT INTO home_build_numbers (job, number, owner, epoch) VALUES ($1, $2, $3, $4)`,
		job, number, replica, epoch); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// MaxReserved is the highest number reserved for a job (0 for none): a new owner starts above it.
func (s *Store) MaxReserved(ctx context.Context, job string) (int, error) {
	var max int
	err := s.Pool.QueryRow(ctx, `SELECT COALESCE(max(number), 0) FROM home_build_numbers WHERE job = $1`, job).Scan(&max)
	return max, err
}
