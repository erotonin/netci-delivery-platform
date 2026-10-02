package cellagent

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// CascReload applies a changed Configuration as Code file to the running controller.
//
// The cell's JCasC comes from a ConfigMap, and Jenkins reads it once, at start. A change was
// otherwise applied at the next restart -- whenever that came, a takeover included -- and a
// restart only to apply it costs the cell its controller for half a minute. The kubelet
// replaces the mounted file when the ConfigMap changes; this notices the content change and
// asks JCasC to apply it, through its token-protected reload endpoint, in place.
//
// A reload that fails is tried again at the next interval: the content is "applied" only once
// Jenkins has accepted it. A file JCasC refuses is not: the same content gets the same answer,
// and each attempt would apply it again up to the same error (JCasC is not transactional). It is
// reported once, and the next change is tried.
type CascReload struct {
	File  string // the mounted jenkins.yaml
	URL   string // the controller, e.g. http://127.0.0.1:8080
	Token string // the controller's CASC_RELOAD_TOKEN; never logged, also not as part of a URL
	Every time.Duration
	HTTP  *http.Client
	Log   *slog.Logger
	// Reloads counts attempts by result; nil in tests that do not look.
	Reloads *prometheus.CounterVec

	applied [sha256.Size]byte
	refused [sha256.Size]byte
}

// errRefused: Jenkins read the file and rejected it.
var errRefused = errors.New("refused")

// NewCascReloads registers the reload counter on r.
func NewCascReloads(r prometheus.Registerer) *prometheus.CounterVec {
	c := prometheus.NewCounterVec(prometheus.CounterOpts{Name: "netci_cell_casc_reloads_total",
		Help: "Configuration as Code reloads asked of the controller after the file changed, by result."}, []string{"result"})
	r.MustRegister(c)
	// At 0 from the start: increase() needs two samples, and the first refusal must alert.
	for _, result := range []string{"applied", "error", "refused"} {
		c.WithLabelValues(result)
	}
	return c
}

// Run watches until ctx is done. What the file holds when it starts is what the controller
// read when it started, so that is not reloaded.
func (r *CascReload) Run(ctx context.Context) {
	r.baseline()
	t := time.NewTicker(r.Every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		r.Check(ctx)
	}
}

func (r *CascReload) baseline() {
	if sum, err := r.sum(); err == nil {
		r.applied = sum
	}
}

// Check reloads once if the file changed since what was last applied. Run calls it; it is not
// safe to call alongside Run.
func (r *CascReload) Check(ctx context.Context) {
	sum, err := r.sum()
	if err != nil {
		r.Log.Warn("cannot read the JCasC file", "file", r.File, "error", err)
		return
	}
	if sum == r.applied || sum == r.refused {
		return
	}
	err = r.reload(ctx)
	if errors.Is(err, errRefused) {
		r.refused = sum
		r.count("refused")
		r.Log.Error("the controller refused the changed JCasC file: it runs its previous configuration, possibly in part changed, "+
			"and would not start on this file; the controller's log has the reason", "file", r.File)
		return
	}
	if err != nil {
		r.count("error")
		r.Log.Warn("the JCasC file changed; the controller did not apply it yet", "file", r.File, "error", err)
		return
	}
	r.applied = sum
	r.count("applied")
	r.Log.Info("the JCasC file changed; the controller applied it in place", "file", r.File)
}

func (r *CascReload) sum() ([sha256.Size]byte, error) {
	b, err := os.ReadFile(r.File)
	if err != nil {
		return [sha256.Size]byte{}, err
	}
	return sha256.Sum256(b), nil
}

func (r *CascReload) reload(ctx context.Context) error {
	u := r.URL + "/reload-configuration-as-code/?" + url.Values{"casc-reload-token": {r.Token}}.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, bytes.NewReader(nil))
	if err != nil {
		return fmt.Errorf("request to %s: invalid", r.URL)
	}
	resp, err := r.HTTP.Do(req)
	if err != nil {
		// A url.Error carries the URL, token included: say what failed without it.
		if ue, ok := err.(*url.Error); ok {
			return fmt.Errorf("POST %s/reload-configuration-as-code/: %v", r.URL, ue.Err)
		}
		return fmt.Errorf("POST %s/reload-configuration-as-code/: failed", r.URL)
	}
	defer resp.Body.Close()
	// The body is never logged: JCasC's message about a refused file can quote its values.
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	switch resp.StatusCode {
	case http.StatusOK:
		// Before configuration-as-code #2907 (2026-09) a refused file was answered 200 with
		// {"status":"error",...}: the lab's cells reported a refused file as applied.
		var answer struct {
			Status string `json:"status"`
		}
		if json.Unmarshal(body, &answer) == nil && answer.Status == "error" {
			return errRefused
		}
		return nil
	case http.StatusInternalServerError:
		return errRefused
	case http.StatusNotFound:
		return fmt.Errorf("the controller has reload by token disabled (CASC_RELOAD_TOKEN unset): HTTP 404")
	case http.StatusUnauthorized:
		return fmt.Errorf("the controller refused the reload token: HTTP 401")
	default:
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
}

func (r *CascReload) count(result string) {
	if r.Reloads != nil {
		r.Reloads.WithLabelValues(result).Inc()
	}
}
