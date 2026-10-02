package cellagent

import (
	"bytes"
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

const reloadToken = "s3cret-reload-token"

type controllerStub struct {
	mu     sync.Mutex
	posts  int
	status []int // answers in turn; then 200
	// errorBody: answer 200 with JCasC's error JSON, as configuration-as-code before #2907 did.
	errorBody bool
}

func (c *controllerStub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if r.Method != http.MethodPost || r.URL.Path != "/reload-configuration-as-code/" || r.URL.Query().Get("casc-reload-token") != reloadToken {
		http.Error(w, "unexpected", http.StatusTeapot)
		return
	}
	c.posts++
	if c.errorBody {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"error","message":"Failed to reload configuration: Invalid configuration elements; password: hunter2"}`))
		return
	}
	if len(c.status) > 0 {
		s := c.status[0]
		c.status = c.status[1:]
		http.Error(w, "no", s)
	}
}

func (c *controllerStub) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.posts
}

// write replaces the file the way the kubelet updates a ConfigMap volume: a new file renamed
// over the old one.
func write(t *testing.T, path, content string) {
	t.Helper()
	tmp := path + ".new"
	if err := os.WriteFile(tmp, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(tmp, path); err != nil {
		t.Fatal(err)
	}
}

func reloader(t *testing.T, url string) (*CascReload, *bytes.Buffer) {
	t.Helper()
	file := filepath.Join(t.TempDir(), "jenkins.yaml")
	write(t, file, "jenkins: {systemMessage: one}\n")
	var logs bytes.Buffer
	r := &CascReload{File: file, URL: url, Token: reloadToken, Every: time.Hour, HTTP: &http.Client{Timeout: time.Second},
		Log: slog.New(slog.NewTextHandler(&logs, nil))}
	r.baseline() // as Run does first
	return r, &logs
}

func TestAChangedFileIsAppliedOnceAndTheStartingOneNot(t *testing.T) {
	stub := &controllerStub{}
	srv := httptest.NewServer(stub)
	defer srv.Close()
	r, _ := reloader(t, srv.URL)
	r.Check(context.Background())
	if n := stub.count(); n != 0 {
		t.Fatalf("%d reloads of what the controller read at its start", n)
	}
	write(t, r.File, "jenkins: {systemMessage: two}\n")
	r.Check(context.Background())
	r.Check(context.Background())
	if n := stub.count(); n != 1 {
		t.Fatalf("%d reloads for one change, want 1", n)
	}
}

func TestAReloadThatFailedIsTriedAgain(t *testing.T) {
	stub := &controllerStub{status: []int{http.StatusServiceUnavailable}}
	srv := httptest.NewServer(stub)
	defer srv.Close()
	r, logs := reloader(t, srv.URL)
	write(t, r.File, "jenkins: {systemMessage: two}\n")
	r.Check(context.Background())
	r.Check(context.Background())
	r.Check(context.Background())
	if n := stub.count(); n != 2 {
		t.Fatalf("%d attempts, want the failed one and one more", n)
	}
	if !strings.Contains(logs.String(), "did not apply it yet") || !strings.Contains(logs.String(), "applied it in place") {
		t.Fatal(logs.String())
	}
}

func TestTheTokenIsNeverLogged(t *testing.T) {
	srv := httptest.NewServer(&controllerStub{})
	srv.Close() // nothing listens: the error is the transport's, which names the URL
	r, logs := reloader(t, srv.URL)
	write(t, r.File, "jenkins: {systemMessage: two}\n")
	r.Check(context.Background())
	if strings.Contains(logs.String(), reloadToken) {
		t.Fatalf("the reload token is in the log: %s", logs.String())
	}
	if !strings.Contains(logs.String(), "did not apply it yet") {
		t.Fatal(logs.String())
	}
}

func TestADisabledReloadSaysWhy(t *testing.T) {
	srv := httptest.NewServer(&controllerStub{status: []int{http.StatusNotFound}})
	defer srv.Close()
	r, logs := reloader(t, srv.URL)
	write(t, r.File, "jenkins: {systemMessage: two}\n")
	r.Check(context.Background())
	if !strings.Contains(logs.String(), "CASC_RELOAD_TOKEN unset") {
		t.Fatal(logs.String())
	}
}

func TestAFileTheControllerRefusesIsNeitherReportedAppliedNorRetried(t *testing.T) {
	for name, stub := range map[string]*controllerStub{
		"200 with an error body (JCasC before #2907)": {errorBody: true},
		"500 (JCasC since #2907)":                     {status: []int{http.StatusInternalServerError, http.StatusInternalServerError}},
	} {
		t.Run(name, func(t *testing.T) {
			srv := httptest.NewServer(stub)
			defer srv.Close()
			r, logs := reloader(t, srv.URL)
			write(t, r.File, "jenkins: {systemMessage: two}\n")
			r.Check(context.Background())
			r.Check(context.Background())
			if n := stub.count(); n != 1 {
				t.Fatalf("%d attempts for one refused file, want 1", n)
			}
			if strings.Contains(logs.String(), "applied it in place") || !strings.Contains(logs.String(), "refused the changed JCasC file") {
				t.Fatal(logs.String())
			}
			if strings.Contains(logs.String(), "hunter2") {
				t.Fatalf("the refusal's body is in the log: %s", logs.String())
			}
			write(t, r.File, "jenkins: {systemMessage: three}\n")
			r.Check(context.Background())
			if n := stub.count(); n != 2 {
				t.Fatalf("a changed file after a refusal: %d attempts, want 2", n)
			}
		})
	}
}
