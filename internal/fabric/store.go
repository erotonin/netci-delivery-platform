// Package fabric runs build sandboxes: warm agent pods bound late to the Jenkins controller that
// needs them, one build each (ADR-061, ADR-064).
package fabric

import (
	"context"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/erotonin/netci-delivery-platform/internal/pgmigrate"
)

//go:embed migrations/*.sql
var migrations embed.FS

// Migrate applies the fabric's migrations.
func Migrate(ctx context.Context, pool *pgxpool.Pool) error {
	return pgmigrate.Apply(ctx, pool, migrations, "migrations")
}

// State of a sandbox.
type State string

const (
	Creating State = "creating" // the row exists, the pod is being created and started
	Warm     State = "warm"     // the pod runs, its bootstrap waits for a binding
	Claimed  State = "claimed"  // given to a controller's agent; the binding is in memory
	Bound    State = "bound"    // the bootstrap took the binding and started the agent
	Released State = "released" // done (or abandoned): its pod is to be deleted
	Deleted  State = "deleted"  // the pod is gone
	Failed   State = "failed"   // something went wrong: its pod is to be deleted
)

var transitions = map[State][]State{
	Creating: {Warm, Failed, Released},
	Warm:     {Claimed, Released, Failed},
	Claimed:  {Bound, Released, Failed},
	Bound:    {Released, Failed},
	Released: {Deleted},
	Failed:   {Deleted},
}

func allowed(from, to State) bool {
	for _, s := range transitions[from] {
		if s == to {
			return true
		}
	}
	return false
}

// Sandbox is one row.
type Sandbox struct {
	ID         uuid.UUID
	Pool       string
	Pod        string
	PodUID     string
	State      State
	Cold       bool
	Cell       string
	Agent      string
	Controller string
	Reason     string
	CreatedAt  time.Time
	ClaimedAt  *time.Time
	BoundAt    *time.Time
}

// Store is the fabric's state in PostgreSQL.
type Store struct {
	Pool *pgxpool.Pool
}

const columns = `id, pool, pod, coalesce(pod_uid, ''), state, cold, coalesce(cell, ''), coalesce(agent, ''),
	coalesce(controller, ''), coalesce(reason, ''), created_at, claimed_at, bound_at`

func scan(row pgx.Row) (*Sandbox, error) {
	var s Sandbox
	var state string
	if err := row.Scan(&s.ID, &s.Pool, &s.Pod, &s.PodUID, &state, &s.Cold, &s.Cell, &s.Agent, &s.Controller,
		&s.Reason, &s.CreatedAt, &s.ClaimedAt, &s.BoundAt); err != nil {
		return nil, err
	}
	s.State = State(state)
	return &s, nil
}

// PodName is a sandbox's pod name, fixed by its id so that creating the pod is idempotent.
func PodName(id uuid.UUID) string {
	return "sbx-" + id.String()[:8] + id.String()[9:13] + id.String()[14:18]
}

// Create adds a sandbox in Creating, to be made warm.
func (s *Store) Create(ctx context.Context, pool string) (*Sandbox, error) {
	id := uuid.New()
	return s.insert(ctx, id, pool, Creating, nil)
}

// Claim is what a controller asks for.
type Claim struct {
	Pool, Cell, Agent, Controller string
}

// ClaimWarm takes the oldest warm sandbox of the pool, or returns nil if there is none.
func (s *Store) ClaimWarm(ctx context.Context, c Claim) (*Sandbox, error) {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	sb, err := scan(tx.QueryRow(ctx, `UPDATE sandboxes SET state = 'claimed', cell = $2, agent = $3, controller = $4, claimed_at = now()
		WHERE id = (SELECT id FROM sandboxes WHERE pool = $1 AND state = 'warm' ORDER BY warm_at LIMIT 1 FOR UPDATE SKIP LOCKED)
		  AND state = 'warm' 
		RETURNING `+columns, c.Pool, c.Cell, c.Agent, c.Controller))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if err := event(ctx, tx, sb.ID, Warm, Claimed, map[string]any{"cell": c.Cell, "agent": c.Agent}); err != nil {
		return nil, err
	}
	return sb, tx.Commit(ctx)
}

// ClaimCold adds a sandbox that is claimed before its pod exists (no warm one was left).
func (s *Store) ClaimCold(ctx context.Context, c Claim) (*Sandbox, error) {
	return s.insert(ctx, uuid.New(), c.Pool, Claimed, &c)
}

func (s *Store) insert(ctx context.Context, id uuid.UUID, pool string, state State, c *Claim) (*Sandbox, error) {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	var cell, agent, controller any
	if c != nil {
		cell, agent, controller = c.Cell, c.Agent, c.Controller
	}
	sb, err := scan(tx.QueryRow(ctx, `INSERT INTO sandboxes (id, pool, pod, state, cold, cell, agent, controller, claimed_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, CASE WHEN $4 = 'claimed' THEN now() END) RETURNING `+columns,
		id, pool, PodName(id), string(state), c != nil, cell, agent, controller))
	if err != nil {
		return nil, err
	}
	if err := event(ctx, tx, id, "", state, map[string]any{"pool": pool}); err != nil {
		return nil, err
	}
	return sb, tx.Commit(ctx)
}

// ErrConflict: the sandbox was not in the expected state.
var ErrConflict = errors.New("sandbox is no longer in the expected state")

// Transition moves a sandbox from `from` to `to` if it is still in `from`.
func (s *Store) Transition(ctx context.Context, id uuid.UUID, from, to State, podUID, reason string) error {
	if !allowed(from, to) {
		return fmt.Errorf("sandbox transition %s -> %s is not allowed", from, to)
	}
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	tag, err := tx.Exec(ctx, `UPDATE sandboxes SET state = $3,
			pod_uid = coalesce(nullif($4, ''), pod_uid),
			reason = coalesce(nullif($5, ''), reason),
			warm_at = CASE WHEN $3 = 'warm' THEN now() ELSE warm_at END,
			bound_at = CASE WHEN $3 = 'bound' THEN now() ELSE bound_at END,
			released_at = CASE WHEN $3 IN ('released', 'failed') THEN now() ELSE released_at END,
			ended_at = CASE WHEN $3 = 'deleted' THEN now() ELSE ended_at END
		WHERE id = $1 AND state = $2`, id, string(from), string(to), podUID, reason)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return ErrConflict
	}
	detail := map[string]any{}
	if reason != "" {
		detail["reason"] = reason
	}
	if err := event(ctx, tx, id, from, to, detail); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// SetPodUID records the pod's UID once it exists (the identity a sandbox proves).
func (s *Store) SetPodUID(ctx context.Context, id uuid.UUID, uid string) error {
	_, err := s.Pool.Exec(ctx, `UPDATE sandboxes SET pod_uid = $2 WHERE id = $1 AND pod_uid IS NULL`, id, uid)
	return err
}

// Get returns one sandbox.
func (s *Store) Get(ctx context.Context, id uuid.UUID) (*Sandbox, error) {
	sb, err := scan(s.Pool.QueryRow(ctx, `SELECT `+columns+` FROM sandboxes WHERE id = $1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	return sb, err
}

// ByPod returns the sandbox whose pod this is.
func (s *Store) ByPod(ctx context.Context, pod string) (*Sandbox, error) {
	sb, err := scan(s.Pool.QueryRow(ctx, `SELECT `+columns+` FROM sandboxes WHERE pod = $1`, pod))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	return sb, err
}

// ErrNotFound: no such sandbox.
var ErrNotFound = errors.New("no such sandbox")

// Live returns every sandbox not yet deleted.
func (s *Store) Live(ctx context.Context) ([]*Sandbox, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+columns+` FROM sandboxes WHERE state <> 'deleted' ORDER BY created_at`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Sandbox
	for rows.Next() {
		sb, err := scan(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, sb)
	}
	return out, rows.Err()
}

func event(ctx context.Context, tx pgx.Tx, id uuid.UUID, from, to State, detail map[string]any) error {
	b, err := json.Marshal(detail)
	if err != nil {
		return err
	}
	var f any
	if from != "" {
		f = string(from)
	}
	_, err = tx.Exec(ctx, `INSERT INTO sandbox_events (sandbox_id, from_state, to_state, detail) VALUES ($1, $2, $3, $4)`, id, f, string(to), b)
	return err
}
