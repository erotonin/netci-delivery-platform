package sandbox

import (
	"context"
	"encoding/json"
	"io"
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

type fakeFabric struct {
	mu       sync.Mutex
	polls    int
	bindAt   int    // binding returned from this poll on
	gone     bool   // 410 for everything
	state    string // what /v1/state says
	tokens   []string
	released chan struct{}
}

func (f *fakeFabric) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.tokens = append(f.tokens, r.Header.Get("Authorization"))
	if f.gone {
		w.WriteHeader(http.StatusGone)
		return
	}
	switch r.URL.Path {
	case "/v1/binding":
		f.polls++
		if f.polls < f.bindAt {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		_ = json.NewEncoder(w).Encode(Binding{Controller: ctrl.URL, Agent: "netci-standard-1", Secret: strings.Repeat("s", 64)})
	case "/v1/state":
		_ = json.NewEncoder(w).Encode(map[string]string{"state": f.state})
	}
}

func (f *fakeFabric) set(state string) {
	f.mu.Lock()
	f.state = state
	f.mu.Unlock()
}

// ctrl is the fake controller; its session can be switched.
var ctrl *httptest.Server

type controller struct {
	mu      sync.Mutex
	session string
}

func (c *controller) ServeHTTP(w http.ResponseWriter, _ *http.Request) {
	c.mu.Lock()
	defer c.mu.Unlock()
	w.Header().Set("X-Jenkins-Session", c.session)
}

func (c *controller) set(s string) {
	c.mu.Lock()
	c.session = s
	c.mu.Unlock()
}

// fakeJava records each start (args and the secret it could read) and runs until killed, or
// exits at once if the file "exit-now" exists.
func fakeJava(t *testing.T, dir string) string {
	script := filepath.Join(dir, "java")
	body := `#!/bin/sh
secret=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-secret" ]; then f="${a#@}"; secret=$(cat "$f"); fi
  prev="$a"
done
echo "$* | secret=$secret" >> ` + filepath.Join(dir, "starts") + `
[ -f ` + filepath.Join(dir, "exit-now") + ` ] && exit 3
exec sleep 300
`
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	return script
}

func starts(dir string) []string {
	b, _ := os.ReadFile(filepath.Join(dir, "starts"))
	var out []string
	for _, l := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if l != "" {
			out = append(out, l)
		}
	}
	return out
}

func boot(t *testing.T, fabric *httptest.Server, dir string) *Bootstrap {
	token := filepath.Join(dir, "token")
	_ = os.WriteFile(token, []byte("projected-token\n"), 0o600)
	return &Bootstrap{FabricURL: fabric.URL, TokenFile: token, Java: fakeJava(t, dir), AgentJar: "/agent.jar",
		WorkDir: "/work", SecretDir: dir, ReadyFile: filepath.Join(dir, "ready"),
		Client: &http.Client{Timeout: 5 * time.Second}, Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		SessionPoll: 50 * time.Millisecond, Retry: 50 * time.Millisecond, SecretTTL: 200 * time.Millisecond}
}

func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	end := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(end) {
			t.Fatalf("timed out: %s", what)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func setup(t *testing.T) (*fakeFabric, *controller, *httptest.Server, string) {
	c := &controller{session: "session-1"}
	ctrl = httptest.NewServer(c)
	t.Cleanup(ctrl.Close)
	f := &fakeFabric{bindAt: 3, state: "bound"}
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	return f, c, srv, t.TempDir()
}

func TestAWarmSandboxRunsTheAgentItIsBoundTo(t *testing.T) {
	f, _, srv, dir := setup(t)
	b := boot(t, srv, dir)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- b.Run(ctx) }()
	eventually(t, "agent started", func() bool { return len(starts(dir)) == 1 })
	got := starts(dir)[0]
	for _, want := range []string{"-jar /agent.jar", "-url " + ctrl.URL, "-name netci-standard-1", "-webSocket", "-workDir /work", "secret=" + strings.Repeat("s", 64)} {
		if !strings.Contains(got, want) {
			t.Fatalf("agent started with %q, missing %q", got, want)
		}
	}
	if strings.Contains(got, "-secret "+strings.Repeat("s", 64)) {
		t.Fatal("the secret is on the command line")
	}
	if _, err := os.Stat(filepath.Join(dir, "ready")); err != nil {
		t.Fatal("not marked ready while warm")
	}
	eventually(t, "secret file removed", func() bool { _, err := os.Stat(filepath.Join(dir, "agent-secret")); return os.IsNotExist(err) })
	f.mu.Lock()
	for _, tok := range f.tokens {
		if tok != "Bearer projected-token" {
			t.Errorf("called the fabric with %q", tok)
		}
	}
	f.mu.Unlock()
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestANewControllerSessionRestartsTheAgent(t *testing.T) {
	_, c, srv, dir := setup(t)
	b := boot(t, srv, dir)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = b.Run(ctx) }()
	eventually(t, "agent started", func() bool { return len(starts(dir)) == 1 })
	time.Sleep(200 * time.Millisecond)
	if len(starts(dir)) != 1 {
		t.Fatal("restarted while the controller stayed the same")
	}
	c.set("") // the controller is down: not a new one
	time.Sleep(200 * time.Millisecond)
	if len(starts(dir)) != 1 {
		t.Fatal("restarted on a blip")
	}
	c.set("session-2") // taken over
	eventually(t, "agent restarted for the new controller", func() bool { return len(starts(dir)) == 2 })
}

func TestTheSandboxStopsWhenItsBuildIsOverAndOnlyThen(t *testing.T) {
	f, _, srv, dir := setup(t)
	_ = os.WriteFile(filepath.Join(dir, "exit-now"), nil, 0o644) // the agent exits by itself
	b := boot(t, srv, dir)
	done := make(chan error, 1)
	go func() { done <- b.Run(context.Background()) }()
	eventually(t, "restarted while still bound", func() bool { return len(starts(dir)) >= 2 })
	f.set("released")
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("did not stop once released")
	}
}

func TestASandboxAlreadyOverNeverStartsAnAgent(t *testing.T) {
	f, _, srv, dir := setup(t)
	f.gone = true
	b := boot(t, srv, dir)
	if err := b.Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(starts(dir)) != 0 {
		t.Fatal("started an agent for a released sandbox")
	}
}
