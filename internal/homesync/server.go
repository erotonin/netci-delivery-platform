package homesync

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// MaxItemBytes bounds one item's config.xml: a job's is a few KiB; a megabyte is a mistake.
const MaxItemBytes = 4 << 20

// MaxBuildBytes bounds one finished build (its directory, gzipped: build.xml, log, flow nodes,
// ADR-078). Kept in PostgreSQL in the lab; a build above it is refused and said so -- a site would
// keep build archives in an object store.
const MaxBuildBytes = 32 << 20

func maxBytes(path string) int64 {
	if strings.HasPrefix(path, "@builds/") {
		return MaxBuildBytes
	}
	return MaxItemBytes
}

// Replicas maps a bearer token to the replica it identifies. Which replica made a change is
// decided here, from the credential, never from anything the request says about itself.
type Replicas struct{ byDigest map[[32]byte]string }

// LoadReplicas reads "replica:token" lines (blank lines and # comments ignored).
func LoadReplicas(path string) (*Replicas, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	r := &Replicas{byDigest: map[[32]byte]string{}}
	for i, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, token, ok := strings.Cut(line, ":")
		if !ok || name == "" || len(token) < 32 {
			return nil, fmt.Errorf("%s line %d: want replica:token with a token of 32+ characters", path, i+1)
		}
		r.byDigest[sha256.Sum256([]byte(token))] = name
	}
	if len(r.byDigest) == 0 {
		return nil, fmt.Errorf("%s names no replica", path)
	}
	return r, nil
}

// Identify returns the replica a bearer token belongs to, or "".
func (r *Replicas) Identify(authorization string) string {
	token, ok := strings.CutPrefix(authorization, "Bearer ")
	if !ok || token == "" {
		return ""
	}
	digest := sha256.Sum256([]byte(token))
	for known, name := range r.byDigest { // constant-time per entry; the set is a handful
		if subtle.ConstantTimeCompare(known[:], digest[:]) == 1 {
			return name
		}
	}
	return ""
}

// Server is the HTTP API every replica's plugin talks to.
type Server struct {
	Store    *Store
	Replicas *Replicas
	Log      *slog.Logger
	feed     *broadcaster
}

// NewServer starts following the database's change notifications for the feed's long polls.
func NewServer(ctx context.Context, store *Store, replicas *Replicas, log *slog.Logger) *Server {
	s := &Server{Store: store, Replicas: replicas, Log: log, feed: newBroadcaster()}
	go s.feed.listen(ctx, store.Pool, log)
	return s
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		if err := s.Store.Pool.Ping(r.Context()); err != nil {
			http.Error(w, "database unreachable", http.StatusServiceUnavailable)
			return
		}
		w.Write([]byte("ok")) //nolint:errcheck
	})
	mux.HandleFunc("GET /v1/items", s.authed(s.getItem))
	mux.HandleFunc("PUT /v1/items", s.authed(s.putItem))
	mux.HandleFunc("DELETE /v1/items", s.authed(s.deleteItem))
	mux.HandleFunc("POST /v1/locks", s.authed(s.lock))
	mux.HandleFunc("DELETE /v1/locks", s.authed(s.unlock))
	mux.HandleFunc("GET /v1/changes", s.authed(s.changes))
	mux.HandleFunc("POST /v1/jobs/claim", s.authed(s.claimJob))
	mux.HandleFunc("GET /v1/jobs/owner", s.authed(s.jobOwner))
	mux.HandleFunc("POST /v1/jobs/reserve", s.authed(s.reserve))
	return mux
}

// jobPath reads ?job= with the rules of an item's full name.
func jobPath(r *http.Request) (string, error) {
	q := r.URL.Query()
	q.Set("path", q.Get("job"))
	r2 := *r
	u := *r.URL
	u.RawQuery = q.Encode()
	r2.URL = &u
	return itemPath(&r2)
}

func (s *Server) claimJob(w http.ResponseWriter, r *http.Request, replica string) {
	job, err := jobPath(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": "BAD_JOB", "message": err.Error()})
		return
	}
	ttl, err := time.ParseDuration(r.URL.Query().Get("ttl"))
	if err != nil {
		ttl = 15 * time.Second
	}
	o, err := s.Store.ClaimJob(r.Context(), job, replica, ttl)
	var owned *OwnedError
	switch {
	case errors.As(err, &owned):
		writeJSON(w, http.StatusLocked, map[string]any{"code": "OWNED", "owner": owned.Ownership.Owner,
			"epoch": owned.Ownership.Epoch, "expiresInMs": owned.Ownership.ExpiresIn})
	case err != nil:
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": "BAD_REQUEST", "message": err.Error()})
	default:
		max, err := s.Store.MaxReserved(r.Context(), job)
		if err != nil {
			s.fail(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"job": o.Job, "owner": o.Owner, "epoch": o.Epoch,
			"expiresInMs": o.ExpiresIn, "maxReserved": max})
	}
}

func (s *Server) jobOwner(w http.ResponseWriter, r *http.Request, _ string) {
	job, err := jobPath(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": "BAD_JOB", "message": err.Error()})
		return
	}
	o, err := s.Store.JobOwner(r.Context(), job)
	if err != nil {
		s.fail(w, err)
		return
	}
	writeJSON(w, http.StatusOK, o)
}

func (s *Server) reserve(w http.ResponseWriter, r *http.Request, replica string) {
	job, err := jobPath(r)
	number, nerr := strconv.Atoi(r.URL.Query().Get("number"))
	epoch, eerr := strconv.ParseInt(r.URL.Query().Get("epoch"), 10, 64)
	if err != nil || nerr != nil || eerr != nil || number < 1 {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": "BAD_REQUEST", "message": "job, number (1+) and epoch are required"})
		return
	}
	err = s.Store.Reserve(r.Context(), job, number, replica, epoch)
	var taken *NumberTakenError
	switch {
	case err == nil:
		writeJSON(w, http.StatusOK, map[string]any{"job": job, "number": number})
	case errors.As(err, &taken):
		writeJSON(w, http.StatusConflict, map[string]any{"code": "NUMBER_TAKEN", "max": taken.Max})
	case errors.Is(err, ErrNotOwner):
		writeJSON(w, http.StatusConflict, map[string]any{"code": "NOT_OWNER", "message": err.Error()})
	default:
		s.fail(w, err)
	}
}

type handler func(w http.ResponseWriter, r *http.Request, replica string)

func (s *Server) authed(h handler) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		replica := s.Replicas.Identify(r.Header.Get("Authorization"))
		if replica == "" {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"code": "UNAUTHORIZED", "message": "a replica's bearer token is required"})
			return
		}
		h(w, r, replica)
	}
}

func itemPath(r *http.Request) (string, error) {
	path := r.URL.Query().Get("path")
	if path == "" || strings.HasPrefix(path, "/") || strings.HasSuffix(path, "/") || strings.Contains(path, "//") ||
		strings.Contains(path, "..") || len(path) > 1024 {
		return "", errors.New("path must be an item's full name, e.g. team/app")
	}
	return path, nil
}

func (s *Server) getItem(w http.ResponseWriter, r *http.Request, _ string) {
	path, err := itemPath(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": "BAD_PATH", "message": err.Error()})
		return
	}
	it, ok, err := s.Store.Get(r.Context(), path)
	if err != nil {
		s.fail(w, err)
		return
	}
	w.Header().Set("X-Version", strconv.FormatInt(it.Version, 10))
	if !ok || it.Deleted {
		w.Header().Set("X-Deleted", strconv.FormatBool(ok && it.Deleted))
		writeJSON(w, http.StatusNotFound, map[string]any{"code": "NO_ITEM", "version": it.Version})
		return
	}
	w.Header().Set("Content-Type", "application/xml")
	w.Header().Set("X-Updated-By", it.UpdatedBy)
	// When, on the database's clock: for a running build's copy, the last time its runner was
	// known alive (ADR-079's fence compares a process's start with it).
	w.Header().Set("X-Updated-At", strconv.FormatInt(it.UpdatedAt.UnixMilli(), 10))
	w.Write(it.Content) //nolint:errcheck
}

// conditions reads If-Match (the version the change was made from) and X-Lock-Token.
func conditions(r *http.Request) (expect, token int64, err error) {
	if expect, err = strconv.ParseInt(r.Header.Get("If-Match"), 10, 64); err != nil || expect < 0 {
		return 0, 0, errors.New("If-Match must be the version the change was made from (0 for a new item)")
	}
	if raw := r.Header.Get("X-Lock-Token"); raw != "" {
		if token, err = strconv.ParseInt(raw, 10, 64); err != nil || token <= 0 {
			return 0, 0, errors.New("X-Lock-Token must be the token Lock returned")
		}
	}
	return expect, token, nil
}

func (s *Server) putItem(w http.ResponseWriter, r *http.Request, replica string) {
	s.change(w, r, replica, false)
}

func (s *Server) deleteItem(w http.ResponseWriter, r *http.Request, replica string) {
	s.change(w, r, replica, true)
}

func (s *Server) change(w http.ResponseWriter, r *http.Request, replica string, del bool) {
	path, err := itemPath(r)
	if err == nil {
		var expect, token int64
		if expect, token, err = conditions(r); err == nil {
			var version int64
			if del {
				version, err = s.Store.Delete(r.Context(), path, expect, replica, token)
			} else {
				var body []byte
				if body, err = io.ReadAll(http.MaxBytesReader(w, r.Body, maxBytes(path))); err == nil {
					if len(body) == 0 {
						err = errors.New("an item's content cannot be empty; delete it instead")
					} else {
						version, err = s.Store.Put(r.Context(), path, body, expect, replica, token)
					}
				}
			}
			if err == nil {
				writeJSON(w, http.StatusOK, map[string]any{"version": version})
				return
			}
			s.refuse(w, err)
			return
		}
	}
	writeJSON(w, http.StatusBadRequest, map[string]any{"code": "BAD_REQUEST", "message": err.Error()})
}

func (s *Server) refuse(w http.ResponseWriter, err error) {
	var conflict *ConflictError
	var locked *LockedError
	switch {
	case errors.As(err, &conflict):
		writeJSON(w, http.StatusConflict, map[string]any{"code": "VERSION_CONFLICT", "current": conflict.Current, "message": err.Error()})
	case errors.As(err, &locked):
		writeJSON(w, http.StatusLocked, map[string]any{"code": "LOCKED", "holder": locked.Holder, "message": err.Error()})
	case errors.Is(err, ErrLockLost):
		writeJSON(w, http.StatusConflict, map[string]any{"code": "LOCK_LOST", "message": err.Error()})
	default:
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			writeJSON(w, http.StatusRequestEntityTooLarge, map[string]any{"code": "TOO_LARGE", "message": err.Error()})
			return
		}
		s.fail(w, err)
	}
}

func (s *Server) lock(w http.ResponseWriter, r *http.Request, replica string) {
	path, err := itemPath(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": "BAD_PATH", "message": err.Error()})
		return
	}
	ttl, err := time.ParseDuration(r.URL.Query().Get("ttl"))
	if err != nil {
		ttl = 30 * time.Second
	}
	token, err := s.Store.Lock(r.Context(), path, replica, ttl)
	if err != nil {
		// Only a bad lifetime is the caller's: anything else is the store's, a 503 like every other
		// store failure. It was a 400 carrying the database's error text, which the soak saw while
		// the store failed over ("homesync answered HTTP 400 to a lock").
		if errors.Is(err, ErrBadTTL) {
			writeJSON(w, http.StatusBadRequest, map[string]any{"code": "BAD_REQUEST", "message": err.Error()})
			return
		}
		s.refuse(w, err)
		return
	}
	// The head comes with the lock: everything up to it must be applied before the edit starts.
	head, err := s.Store.Head(r.Context())
	if err != nil {
		s.fail(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"token": token, "head": head})
}

func (s *Server) unlock(w http.ResponseWriter, r *http.Request, replica string) {
	path, err := itemPath(r)
	token, terr := strconv.ParseInt(r.URL.Query().Get("token"), 10, 64)
	if err != nil || terr != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": "BAD_REQUEST", "message": "path and token are required"})
		return
	}
	if err := s.Store.Unlock(r.Context(), path, replica, token); err != nil {
		s.fail(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// changes answers at once when there is something after `after`, and otherwise waits up to
// `wait` (at most 30 s) for the next notification -- a replica learns of a change within the
// round trip, without polling the database.
func (s *Server) changes(w http.ResponseWriter, r *http.Request, _ string) {
	after, err := strconv.ParseInt(r.URL.Query().Get("after"), 10, 64)
	if err != nil || after < 0 {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": "BAD_REQUEST", "message": "after must be a seq (0 for the start)"})
		return
	}
	wait, err := time.ParseDuration(r.URL.Query().Get("wait"))
	if err != nil || wait < 0 {
		wait = 0
	}
	if wait > 30*time.Second {
		wait = 30 * time.Second
	}
	deadline := time.Now().Add(wait)
	for {
		woken := s.feed.subscribe()
		list, head, err := s.Store.Changes(r.Context(), after, 1000)
		if err != nil {
			s.fail(w, err)
			return
		}
		if len(list) > 0 || time.Now().After(deadline) {
			if list == nil {
				list = []Change{}
			}
			writeJSON(w, http.StatusOK, map[string]any{"changes": list, "head": head})
			return
		}
		select {
		case <-woken:
		case <-time.After(time.Until(deadline)):
		case <-r.Context().Done():
			return
		}
	}
}

func (s *Server) fail(w http.ResponseWriter, err error) {
	s.Log.Error("homesync request failed", "error", err)
	writeJSON(w, http.StatusServiceUnavailable, map[string]any{"code": "UNAVAILABLE", "message": "the store could not answer"})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v) //nolint:errcheck
}

// broadcaster wakes every waiting long poll when the database announces a change. A
// notification is a hint, not the data: a woken poll reads the feed itself, so a missed
// notification costs at most the poll's wait.
type broadcaster struct {
	mu   sync.Mutex
	wake chan struct{}
}

func newBroadcaster() *broadcaster { return &broadcaster{wake: make(chan struct{})} }

func (b *broadcaster) subscribe() <-chan struct{} {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.wake
}

func (b *broadcaster) fire() {
	b.mu.Lock()
	close(b.wake)
	b.wake = make(chan struct{})
	b.mu.Unlock()
}

func (b *broadcaster) listen(ctx context.Context, pool *pgxpool.Pool, log *slog.Logger) {
	for ctx.Err() == nil {
		conn, err := pool.Acquire(ctx)
		if err == nil {
			if _, err = conn.Exec(ctx, "LISTEN "+Notify); err == nil {
				b.fire() // anything committed while not listening is read by the woken polls
				for err == nil {
					if _, err = conn.Conn().WaitForNotification(ctx); err == nil {
						b.fire()
					}
				}
			}
			// Closed, not returned: a pooled connection still LISTENing would hand its
			// notifications to whichever query borrowed it next.
			conn.Conn().Close(context.Background()) //nolint:errcheck
			conn.Release()
		}
		if ctx.Err() == nil {
			log.Warn("homesync: change notifications interrupted, reconnecting", "error", err)
			select {
			case <-ctx.Done():
			case <-time.After(time.Second):
			}
		}
	}
}
