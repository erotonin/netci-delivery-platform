package runqueue

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"
)

// OnceResult says whether a guarded block may run (ADR-065).
type OnceResult struct {
	First      bool      `json:"first"`
	RecordedAt time.Time `json:"recordedAt"`
}

// Once records (client, scope, key) for nonce. It answers First when this is the first record,
// or a retry of the same attempt (same nonce); otherwise the block started before, under
// another attempt, and must not run again.
func (s *Store) Once(ctx context.Context, client, scope, key, nonce string) (OnceResult, error) {
	// A concurrent first record commits while this statement waits on the conflict, and its
	// snapshot may not show that row: no row comes back. The next statement sees it.
	for attempt := 0; ; attempt++ {
		r, err := s.once(ctx, client, scope, key, nonce)
		if errors.Is(err, pgx.ErrNoRows) && attempt < 3 {
			continue
		}
		return r, err
	}
}

func (s *Store) once(ctx context.Context, client, scope, key, nonce string) (OnceResult, error) {
	var r OnceResult
	var recorded string
	err := s.Pool.QueryRow(ctx, `WITH ins AS (
			INSERT INTO once_markers (client, scope, key, nonce) VALUES ($1, $2, $3, $4)
			ON CONFLICT (client, scope, key) DO NOTHING RETURNING nonce, created_at)
		SELECT nonce, created_at FROM ins
		UNION ALL
		SELECT nonce, created_at FROM once_markers WHERE client = $1 AND scope = $2 AND key = $3
		LIMIT 1`, client, scope, key, nonce).Scan(&recorded, &r.RecordedAt)
	if err != nil {
		return r, err
	}
	r.First = recorded == nonce
	return r, nil
}

type onceRequest struct {
	Scope string `json:"scope"`
	Key   string `json:"key"`
	Nonce string `json:"nonce"`
}

// once is POST /v1/once: 200 {first: true} when the block may run, 409 {first: false} when it
// started before under another attempt.
func (in *Intake) once(w http.ResponseWriter, r *http.Request) {
	client, ok := in.client(w, r)
	if !ok {
		return
	}
	if !in.Config.Clients[client].Once {
		problem(w, http.StatusForbidden, "this client may not record once markers")
		return
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, 8<<10))
	if err != nil {
		problem(w, http.StatusBadRequest, "unreadable body")
		return
	}
	var req onceRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		problem(w, http.StatusBadRequest, "the body must be a JSON object")
		return
	}
	switch {
	case req.Scope == "" || len(req.Scope) > 1024:
		problem(w, http.StatusBadRequest, "scope must be 1 to 1024 characters")
		return
	case req.Key == "" || len(req.Key) > 256:
		problem(w, http.StatusBadRequest, "key must be 1 to 256 characters")
		return
	case len(req.Nonce) < 16 || len(req.Nonce) > 128:
		problem(w, http.StatusBadRequest, "nonce must be 16 to 128 characters")
		return
	}
	res, err := in.Store.Once(r.Context(), client, req.Scope, req.Key, req.Nonce)
	if err != nil {
		// Unknown is not "first": the block must not run on a guess (fail closed).
		in.Log.Error("once", "client", client, "scope", req.Scope, "key", req.Key, "error", err)
		problem(w, http.StatusServiceUnavailable, "cannot tell whether this block ran before; it must not run now")
		return
	}
	status := http.StatusOK
	if !res.First {
		status = http.StatusConflict
		in.Log.Warn("a guarded block was started again after it had started under another attempt",
			"client", client, "scope", req.Scope, "key", req.Key, "first_recorded", res.RecordedAt)
	}
	writeJSON(w, status, res)
}
