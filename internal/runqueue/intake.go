package runqueue

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
)

// Intake is the HTTP API through which runs enter the queue.
//
//	POST /v1/runs        {"job", "parameters", "idempotencyKey"}  -> 202 (new) / 200 (same key)
//	GET  /v1/runs/{id}                                            -> the run and its history
//	POST /v1/hooks/{name}  a GitLab or GitHub webhook; see Hook
//	POST /v1/once          a block that must not run twice records itself first (ADR-065)
//
// The caller is decided from its bearer token, the cell from the job; a request that names
// either (or any other field the server owns) is answered 422, never quietly ignored.
type Intake struct {
	Store   *Store
	Config  *Config
	Log     *slog.Logger
	Metrics *Metrics
}

const maxIntakeBody = 256 << 10

var serverOwned = []string{"client", "cell", "id", "state", "session", "build", "buildNumber", "requestedBy", "acceptedAt"}

type submit struct {
	Job            string            `json:"job"`
	Parameters     map[string]string `json:"parameters"`
	IdempotencyKey string            `json:"idempotencyKey"`
}

// Handler routes the API.
func (in *Intake) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/runs", in.create)
	mux.HandleFunc("GET /v1/runs/{id}", in.get)
	mux.HandleFunc("POST /v1/hooks/{name}", in.hook)
	mux.HandleFunc("POST /v1/once", in.once)
	return mux
}

func (in *Intake) client(w http.ResponseWriter, r *http.Request) (string, bool) {
	h := r.Header.Get("Authorization")
	token, ok := strings.CutPrefix(h, "Bearer ")
	if !ok || token == "" {
		w.Header().Set("WWW-Authenticate", `Bearer realm="netci"`)
		problem(w, http.StatusUnauthorized, "a bearer token is required")
		return "", false
	}
	name, ok := in.Config.Client(token)
	if !ok {
		problem(w, http.StatusUnauthorized, "unknown token")
		return "", false
	}
	return name, true
}

func (in *Intake) create(w http.ResponseWriter, r *http.Request) {
	client, ok := in.client(w, r)
	if !ok {
		return
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, maxIntakeBody+1))
	if err != nil || len(raw) > maxIntakeBody {
		problem(w, http.StatusRequestEntityTooLarge, "the body must be at most 256 KiB")
		return
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		problem(w, http.StatusBadRequest, "the body must be a JSON object")
		return
	}
	for _, f := range serverOwned {
		if _, named := fields[f]; named {
			problem(w, http.StatusUnprocessableEntity, "field "+f+" is decided by the server and may not be sent")
			return
		}
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var s submit
	if err := dec.Decode(&s); err != nil {
		problem(w, http.StatusBadRequest, "invalid body: "+err.Error())
		return
	}
	if msg := validJob(s.Job); msg != "" {
		problem(w, http.StatusBadRequest, msg)
		return
	}
	if len(s.Parameters) > 200 || len(s.IdempotencyKey) > 200 {
		problem(w, http.StatusBadRequest, "at most 200 parameters, and an idempotency key of at most 200 characters")
		return
	}
	for name := range s.Parameters {
		if name == "" || len(name) > 128 {
			problem(w, http.StatusBadRequest, "parameter names must be 1 to 128 characters")
			return
		}
	}
	if !in.Config.MayTrigger(client, s.Job) {
		problem(w, http.StatusForbidden, "this client may not trigger "+s.Job)
		return
	}
	cell, ok := in.Config.CellFor(s.Job)
	if !ok {
		problem(w, http.StatusUnprocessableEntity, "no cell owns job "+s.Job)
		return
	}
	run, created, err := in.Store.Accept(r.Context(), NewRun{Client: client, IdempotencyKey: s.IdempotencyKey, Cell: cell, Job: s.Job, Parameters: s.Parameters})
	switch {
	case errors.Is(err, ErrIdempotencyMismatch):
		problem(w, http.StatusConflict, err.Error())
		return
	case err != nil:
		// Not accepted means not acknowledged: the caller must retry, and with the same key
		// that cannot make a second run.
		in.Log.Error("accept run", "client", client, "job", s.Job, "error", err)
		problem(w, http.StatusServiceUnavailable, "the run was not accepted; retry with the same idempotency key")
		return
	}
	status := http.StatusOK
	if created {
		status = http.StatusAccepted
		in.Metrics.accepted.WithLabelValues(cell, client).Inc()
	}
	w.Header().Set("Location", "/v1/runs/"+run.ID.String())
	writeJSON(w, status, view(run, nil))
}

func (in *Intake) get(w http.ResponseWriter, r *http.Request) {
	client, ok := in.client(w, r)
	if !ok {
		return
	}
	id, err := uuid.Parse(r.PathValue("id"))
	if err != nil {
		problem(w, http.StatusNotFound, "no such run")
		return
	}
	run, err := in.Store.Get(r.Context(), client, id)
	if errors.Is(err, ErrNotFound) {
		problem(w, http.StatusNotFound, "no such run")
		return
	}
	if err != nil {
		problem(w, http.StatusServiceUnavailable, "the queue cannot be read now")
		return
	}
	events, err := in.Store.Events(r.Context(), id)
	if err != nil {
		problem(w, http.StatusServiceUnavailable, "the queue cannot be read now")
		return
	}
	writeJSON(w, http.StatusOK, view(run, events))
}

func validJob(job string) string {
	switch {
	case job == "" || len(job) > 512:
		return "job must be a full name of 1 to 512 characters"
	case strings.HasPrefix(job, "/") || strings.HasSuffix(job, "/") || strings.Contains(job, "//"):
		return "job must be a full name such as folder/name"
	}
	for _, seg := range strings.Split(job, "/") {
		if seg == "." || seg == ".." {
			return "job must not contain . or .. segments"
		}
	}
	return ""
}

type runView struct {
	ID          string            `json:"id"`
	Job         string            `json:"job"`
	Cell        string            `json:"cell"`
	Parameters  map[string]string `json:"parameters"`
	State       State             `json:"state"`
	BuildNumber int               `json:"buildNumber,omitempty"`
	BuildURL    string            `json:"buildUrl,omitempty"`
	Result      string            `json:"result,omitempty"`
	Attempts    int               `json:"attempts,omitempty"`
	LastError   string            `json:"lastError,omitempty"`
	AcceptedAt  time.Time         `json:"acceptedAt"`
	StartedAt   *time.Time        `json:"startedAt,omitempty"`
	FinishedAt  *time.Time        `json:"finishedAt,omitempty"`
	History     []eventView       `json:"history,omitempty"`
}

type eventView struct {
	At     time.Time      `json:"at"`
	To     State          `json:"to"`
	Detail map[string]any `json:"detail,omitempty"`
}

func view(r *Run, events []Event) runView {
	v := runView{ID: r.ID.String(), Job: r.Job, Cell: r.Cell, Parameters: r.Parameters, State: r.State,
		BuildNumber: r.BuildNumber, BuildURL: r.BuildURL, Result: r.Result, Attempts: r.Attempts, LastError: r.LastError,
		AcceptedAt: r.AcceptedAt, StartedAt: r.StartedAt, FinishedAt: r.FinishedAt}
	for _, e := range events {
		v.History = append(v.History, eventView{At: e.At, To: e.To, Detail: e.Detail})
	}
	return v
}

func problem(w http.ResponseWriter, status int, detail string) {
	w.Header().Set("Content-Type", "application/problem+json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"status": status, "title": http.StatusText(status), "detail": detail})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
