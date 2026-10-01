package runqueue

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// State of a run. The only transitions are those in transitions; anything else is refused by
// the store, whoever asks.
type State string

const (
	Accepted   State = "accepted"   // durable, not yet in a controller's queue
	Dispatched State = "dispatched" // in a controller's queue (or being taken by an executor)
	Started    State = "started"    // a build exists
	Finished   State = "finished"   // the build ended; Result says how
	Cancelled  State = "cancelled"  // gone from the queue of the controller that had it, never started: someone cancelled it in Jenkins
	Refused    State = "refused"    // the controller will not run it (job gone, parameters refused, no permission)
)

var transitions = map[State][]State{
	Accepted:   {Dispatched, Started, Refused},
	Dispatched: {Dispatched, Started, Cancelled, Refused},
	Started:    {Started, Finished},
}

func allowed(from, to State) bool {
	for _, s := range transitions[from] {
		if s == to {
			return true
		}
	}
	return false
}

// Run is one row.
type Run struct {
	ID             uuid.UUID
	Client         string
	IdempotencyKey string
	Cell           string
	Job            string
	Parameters     map[string]string
	State          State
	Session        string
	QueueID        int64
	BuildNumber    int
	BuildURL       string
	Result         string
	Attempts       int
	LastError      string
	AcceptedAt     time.Time
	DispatchedAt   *time.Time
	StartedAt      *time.Time
	FinishedAt     *time.Time
}

// Store is the queue in PostgreSQL.
type Store struct {
	Pool *pgxpool.Pool
}

const runColumns = `id, client, coalesce(idempotency_key, ''), cell, job, parameters, state, coalesce(session, ''),
	coalesce(queue_id, 0), coalesce(build_number, 0), coalesce(build_url, ''), coalesce(result, ''), attempts,
	coalesce(last_error, ''), accepted_at, dispatched_at, started_at, finished_at`

func scanRun(row pgx.Row) (*Run, error) {
	var r Run
	var params []byte
	var state string
	if err := row.Scan(&r.ID, &r.Client, &r.IdempotencyKey, &r.Cell, &r.Job, &params, &state, &r.Session,
		&r.QueueID, &r.BuildNumber, &r.BuildURL, &r.Result, &r.Attempts, &r.LastError,
		&r.AcceptedAt, &r.DispatchedAt, &r.StartedAt, &r.FinishedAt); err != nil {
		return nil, err
	}
	r.State = State(state)
	if err := json.Unmarshal(params, &r.Parameters); err != nil {
		return nil, err
	}
	return &r, nil
}

// NewRun is what intake accepts; Client and Cell are the server's, never the request's.
type NewRun struct {
	Client         string
	IdempotencyKey string
	Cell           string
	Job            string
	Parameters     map[string]string
}

// ErrIdempotencyMismatch: the key was used before for a different job or parameters.
var ErrIdempotencyMismatch = errors.New("idempotency key already used for a different run")

// Accept stores a run. With an idempotency key already used by this client for the same job
// and parameters, it returns that run and created=false; for a different one, an error.
func (s *Store) Accept(ctx context.Context, n NewRun) (*Run, bool, error) {
	if n.Parameters == nil {
		n.Parameters = map[string]string{}
	}
	params, err := json.Marshal(n.Parameters)
	if err != nil {
		return nil, false, err
	}
	var key any
	if n.IdempotencyKey != "" {
		key = n.IdempotencyKey
	}
	id := uuid.New()
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return nil, false, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	tag, err := tx.Exec(ctx, `INSERT INTO runs (id, client, idempotency_key, cell, job, parameters)
		VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT ON CONSTRAINT runs_idempotency DO NOTHING`,
		id, n.Client, key, n.Cell, n.Job, params)
	if err != nil {
		return nil, false, err
	}
	if tag.RowsAffected() == 0 {
		existing, err := scanRun(tx.QueryRow(ctx, `SELECT `+runColumns+` FROM runs WHERE client = $1 AND idempotency_key = $2`, n.Client, n.IdempotencyKey))
		if err != nil {
			return nil, false, err
		}
		if existing.Job != n.Job || existing.Cell != n.Cell || !sameParams(existing.Parameters, n.Parameters) {
			return nil, false, ErrIdempotencyMismatch
		}
		return existing, false, tx.Commit(ctx)
	}
	if err := event(ctx, tx, id, "", Accepted, map[string]any{"client": n.Client}); err != nil {
		return nil, false, err
	}
	r, err := scanRun(tx.QueryRow(ctx, `SELECT `+runColumns+` FROM runs WHERE id = $1`, id))
	if err != nil {
		return nil, false, err
	}
	return r, true, tx.Commit(ctx)
}

func sameParams(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if w, ok := b[k]; !ok || w != v {
			return false
		}
	}
	return true
}

// ErrNotFound is returned for a run that does not exist or is not the caller's.
var ErrNotFound = errors.New("run not found")

// Get returns a run of client's.
func (s *Store) Get(ctx context.Context, client string, id uuid.UUID) (*Run, error) {
	r, err := scanRun(s.Pool.QueryRow(ctx, `SELECT `+runColumns+` FROM runs WHERE id = $1 AND client = $2`, id, client))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	return r, err
}

// Claim takes up to limit runs of a cell in one of states whose next attempt is due, and
// pushes their next attempt lease into the future, so another dispatcher skips them meanwhile.
// It is a lease, not a lock: a dispatcher that dies loses its claim when the lease ends, and
// dispatch is idempotent, so a run handled twice is still dispatched once.
func (s *Store) Claim(ctx context.Context, cell string, states []State, limit int, lease time.Duration) ([]*Run, error) {
	if limit <= 0 {
		return nil, nil
	}
	names := make([]string, len(states))
	for i, st := range states {
		names[i] = string(st)
	}
	rows, err := s.Pool.Query(ctx, `UPDATE runs SET next_attempt_at = now() + $4 * interval '1 millisecond'
		WHERE id IN (SELECT id FROM runs WHERE cell = $1 AND state = ANY($2) AND next_attempt_at <= now()
		             ORDER BY accepted_at LIMIT $3 FOR UPDATE SKIP LOCKED)
		RETURNING `+runColumns, cell, names, limit, lease.Milliseconds())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Run
	for rows.Next() {
		r, err := scanRun(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// Outstanding counts a cell's runs in its controller's queue (dispatched, not yet started).
func (s *Store) Outstanding(ctx context.Context, cell string) (int, error) {
	var n int
	err := s.Pool.QueryRow(ctx, `SELECT count(*) FROM runs WHERE cell = $1 AND state = 'dispatched'`, cell).Scan(&n)
	return n, err
}

// Update is one transition and what came with it. Zero fields are left as they are.
type Update struct {
	To          State
	Session     string
	QueueID     int64
	BuildNumber int
	BuildURL    string
	Result      string
	NextAttempt time.Duration
	Detail      map[string]any
}

// ErrConflict: the run was not in the expected state (another dispatcher moved it on).
var ErrConflict = errors.New("run is no longer in the expected state")

// Transition moves a run from `from` to u.To if it is still in `from`, and records the event.
func (s *Store) Transition(ctx context.Context, id uuid.UUID, from State, u Update) error {
	if !allowed(from, u.To) {
		return fmt.Errorf("transition %s -> %s is not allowed", from, u.To)
	}
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	tag, err := tx.Exec(ctx, `UPDATE runs SET
			state = $3,
			session = coalesce(nullif($4, ''), session),
			queue_id = coalesce(nullif($5, 0), queue_id),
			build_number = coalesce(nullif($6, 0), build_number),
			build_url = coalesce(nullif($7, ''), build_url),
			result = coalesce(nullif($8, ''), result),
			last_error = NULL, attempts = 0,
			dispatched_at = CASE WHEN $3 = 'dispatched' AND dispatched_at IS NULL THEN now() ELSE dispatched_at END,
			started_at    = CASE WHEN $3 IN ('started', 'finished') AND started_at IS NULL THEN now() ELSE started_at END,
			finished_at   = CASE WHEN $3 IN ('finished', 'cancelled', 'refused') THEN now() ELSE finished_at END,
			next_attempt_at = now() + $9 * interval '1 millisecond'
		WHERE id = $1 AND state = $2`,
		id, string(from), string(u.To), u.Session, u.QueueID, u.BuildNumber, u.BuildURL, u.Result, u.NextAttempt.Milliseconds())
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return ErrConflict
	}
	if from != u.To || len(u.Detail) > 0 {
		if err := event(ctx, tx, id, from, u.To, u.Detail); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

// Retry records a failed attempt and when to try again; the state is unchanged.
func (s *Store) Retry(ctx context.Context, id uuid.UUID, state State, cause error, after time.Duration) error {
	msg := cause.Error()
	if len(msg) > 500 {
		msg = msg[:500]
	}
	_, err := s.Pool.Exec(ctx, `UPDATE runs SET attempts = attempts + 1, last_error = $3,
		next_attempt_at = now() + $4 * interval '1 millisecond' WHERE id = $1 AND state = $2`,
		id, string(state), msg, after.Milliseconds())
	return err
}

// Events returns a run's history, oldest first.
func (s *Store) Events(ctx context.Context, id uuid.UUID) ([]Event, error) {
	rows, err := s.Pool.Query(ctx, `SELECT at, coalesce(from_state, ''), to_state, detail FROM run_events WHERE run_id = $1 ORDER BY id`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Event
	for rows.Next() {
		var e Event
		var from, to string
		var detail []byte
		if err := rows.Scan(&e.At, &from, &to, &detail); err != nil {
			return nil, err
		}
		e.From, e.To = State(from), State(to)
		_ = json.Unmarshal(detail, &e.Detail)
		out = append(out, e)
	}
	return out, rows.Err()
}

// Event is one recorded transition.
type Event struct {
	At     time.Time
	From   State
	To     State
	Detail map[string]any
}

func event(ctx context.Context, tx pgx.Tx, id uuid.UUID, from, to State, detail map[string]any) error {
	if detail == nil {
		detail = map[string]any{}
	}
	b, err := json.Marshal(detail)
	if err != nil {
		return err
	}
	var fromArg any
	if from != "" {
		fromArg = string(from)
	}
	_, err = tx.Exec(ctx, `INSERT INTO run_events (run_id, from_state, to_state, detail) VALUES ($1, $2, $3, $4)`, id, fromArg, string(to), b)
	return err
}

// IsUnavailable reports errors that mean the database could not be reached.
func IsUnavailable(err error) bool {
	var pgErr *pgconn.PgError
	return err != nil && !errors.As(err, &pgErr)
}
