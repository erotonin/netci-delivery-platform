package runqueue

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Controller is a cell's Jenkins, reached through the netCI plugin (ADR-063).
type Controller interface {
	// Dispatch returns where the run is, scheduling it if it is nowhere. Idempotent.
	Dispatch(ctx context.Context, r *Run, requestedBy string) (*Status, error)
	// Lookup returns where the run is, or found=false (with the controller's session).
	Lookup(ctx context.Context, r *Run) (st *Status, found bool, err error)
}

// Status is the plugin's answer.
type Status struct {
	State   string `json:"state"` // queued | starting | started | absent
	Created bool   `json:"created"`
	Session string `json:"session"`
	QueueID int64  `json:"queueId"`
	Build   *struct {
		Number   int    `json:"number"`
		Building bool   `json:"building"`
		Result   string `json:"result"`
		URL      string `json:"url"`
	} `json:"build"`
	Error string `json:"error"`
}

// PermanentError: the controller will never run this request as it stands (unknown job,
// parameters refused, no permission, job disabled). Retrying is pointless.
type PermanentError struct {
	Status  int
	Message string
}

func (e *PermanentError) Error() string {
	return fmt.Sprintf("controller refused the run (HTTP %d): %s", e.Status, e.Message)
}

// Jenkins talks to one controller's plugin with a service account's API token (API tokens are
// exempt from Jenkins' CSRF crumb).
type Jenkins struct {
	BaseURL string // e.g. http://jenkins.cell-b.svc:8080
	User    string
	Token   string
	Client  *http.Client
}

func (j *Jenkins) Dispatch(ctx context.Context, r *Run, requestedBy string) (*Status, error) {
	body, err := json.Marshal(map[string]any{
		"job": r.Job, "runId": r.ID.String(), "parameters": r.Parameters,
		"notBefore": notBefore(r), "requestedBy": requestedBy,
	})
	if err != nil {
		return nil, err
	}
	st, code, err := j.do(ctx, http.MethodPost, "/netci/dispatch", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	if code != http.StatusOK {
		return nil, classify(code, st)
	}
	return st, nil
}

func (j *Jenkins) Lookup(ctx context.Context, r *Run) (*Status, bool, error) {
	q := url.Values{"job": {r.Job}, "runId": {r.ID.String()}, "notBefore": {strconv.FormatInt(notBefore(r), 10)}}
	st, code, err := j.do(ctx, http.MethodGet, "/netci/run?"+q.Encode(), nil)
	if err != nil {
		return nil, false, err
	}
	switch {
	case code == http.StatusOK:
		return st, true, nil
	case code == http.StatusNotFound && st != nil && st.State == "absent":
		return st, false, nil
	default:
		return nil, false, classify(code, st)
	}
}

// notBefore bounds the controller's search of a job's builds: nothing of this run can have
// been scheduled before it was accepted. The margin covers clocks that disagree.
func notBefore(r *Run) int64 {
	return r.AcceptedAt.Add(-10 * time.Minute).UnixMilli()
}

// classify: only the plugin's own answer can refuse a run for good. An HTML 404 (plugin not
// installed), a 403 from Jenkins itself (the service account lacks a permission) or a 503
// while starting are configuration or timing, fixed outside the run: retried, never refused.
func classify(code int, st *Status) error {
	if st == nil {
		return fmt.Errorf("controller answered HTTP %d without the netCI plugin's answer", code)
	}
	msg := st.Error
	switch code {
	case http.StatusBadRequest, http.StatusForbidden, http.StatusNotFound, http.StatusConflict:
		return &PermanentError{Status: code, Message: msg}
	default:
		return fmt.Errorf("controller answered HTTP %d: %s", code, msg)
	}
}

func (j *Jenkins) do(ctx context.Context, method, path string, body io.Reader) (*Status, int, error) {
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimSuffix(j.BaseURL, "/")+path, body)
	if err != nil {
		return nil, 0, err
	}
	req.SetBasicAuth(j.User, j.Token)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := j.Client.Do(req)
	if err != nil {
		var ue *url.Error
		if errors.As(err, &ue) {
			return nil, 0, fmt.Errorf("%s %s: %w", method, ue.URL, ue.Err)
		}
		return nil, 0, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var st Status
	if len(data) > 0 && data[0] == '{' {
		if err := json.Unmarshal(data, &st); err != nil {
			return nil, resp.StatusCode, fmt.Errorf("controller answered something that is not the plugin's JSON: %w", err)
		}
		return &st, resp.StatusCode, nil
	}
	// Not the plugin: Jenkins itself (login page, 503 while starting) or a proxy.
	return nil, resp.StatusCode, nil
}
