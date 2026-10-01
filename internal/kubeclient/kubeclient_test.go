package kubeclient

import (
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"k8s.io/client-go/rest"
)

// deadable is a listener whose accepted connections can be made to behave like one to a machine
// that lost its power: nothing is answered and nothing is closed.
type deadable struct {
	net.Listener
	mu       sync.Mutex
	conns    []*deadConn
	accepted atomic.Int32
}

func (l *deadable) Accept() (net.Conn, error) {
	c, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	l.accepted.Add(1)
	dc := &deadConn{Conn: c}
	l.mu.Lock()
	l.conns = append(l.conns, dc)
	l.mu.Unlock()
	return dc, nil
}

// powerOff makes every connection open so far silent.
func (l *deadable) powerOff() {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, c := range l.conns {
		c.dead.Store(true)
	}
}

type deadConn struct {
	net.Conn
	dead atomic.Bool
}

// Read swallows what a dead connection receives, until the client gives up and closes it (a
// connection the test never releases would hang the server's Close).
func (c *deadConn) Read(p []byte) (int, error) {
	for {
		n, err := c.Conn.Read(p)
		if err != nil || !c.dead.Load() {
			return n, err
		}
	}
}

func server(t *testing.T) (*deadable, *rest.Config, *atomic.Int32) {
	t.Helper()
	puts := &atomic.Int32{}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPut {
			puts.Add(1)
		}
		if r.URL.Path == "/slow" {
			time.Sleep(100 * time.Millisecond)
		}
		_, _ = io.WriteString(w, `{"ok":true}`)
	}))
	l := &deadable{Listener: srv.Listener}
	srv.Listener = l
	srv.Start()
	t.Cleanup(srv.Close)
	return l, &rest.Config{Host: srv.URL, Timeout: 2 * time.Second}, puts
}

func get(t *testing.T, c *http.Client, url string) error {
	t.Helper()
	resp, err := c.Get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, err = io.ReadAll(resp.Body)
	return err
}

func TestAReadOnAPooledConnectionToADeadMachineIsAnsweredOnANewOne(t *testing.T) {
	l, base, _ := server(t)
	c, err := rest.HTTPClientFor(Config(base, 200*time.Millisecond))
	if err != nil {
		t.Fatal(err)
	}
	// Three pooled connections, as a busy client has; all three lead to the machine that goes.
	var wg sync.WaitGroup
	for i := 0; i < MaxAttempts; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); _ = get(t, c, base.Host+"/slow") }()
	}
	wg.Wait()
	l.powerOff()
	start := time.Now()
	if err := get(t, c, base.Host+"/api"); err != nil {
		t.Fatalf("the read failed instead of moving to a new connection: %v", err)
	}
	if took := time.Since(start); took > time.Second {
		t.Fatalf("the read took %s: the dead connection cost more than one attempt", took)
	}
	if n := l.accepted.Load(); n != MaxAttempts+1 {
		t.Fatalf("%d connections accepted, want %d: the retry did not dial afresh", n, MaxAttempts+1)
	}
}

// Without the package, the same client waits for its whole timeout on the dead connection.
func TestWithoutItTheReadWaitsForTheWholeTimeout(t *testing.T) {
	l, base, _ := server(t)
	cfg := rest.CopyConfig(base)
	cfg.Timeout = 600 * time.Millisecond
	c, err := rest.HTTPClientFor(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := get(t, c, base.Host+"/api"); err != nil {
		t.Fatal(err)
	}
	l.powerOff()
	if err := get(t, c, base.Host+"/api"); err == nil {
		t.Fatal("the plain client was answered on a dead connection: the test does not reproduce the failure")
	}
}

func TestAWriteIsNotRepeatedButTheNextGoesToANewConnection(t *testing.T) {
	l, base, puts := server(t)
	c, err := rest.HTTPClientFor(Config(base, 200*time.Millisecond))
	if err != nil {
		t.Fatal(err)
	}
	if err := get(t, c, base.Host+"/api"); err != nil {
		t.Fatal(err)
	}
	l.powerOff()
	put := func() error {
		req, _ := http.NewRequest(http.MethodPut, base.Host+"/lease", strings.NewReader(`{}`))
		resp, err := c.Do(req)
		if err == nil {
			resp.Body.Close()
		}
		return err
	}
	if err := put(); !errors.Is(err, ErrAttemptTimeout) {
		t.Fatalf("a write on the dead connection: %v, want ErrAttemptTimeout", err)
	}
	if n := puts.Load(); n != 0 {
		t.Fatalf("the write reached the server %d times through a dead connection", n)
	}
	if err := put(); err != nil {
		t.Fatalf("the next write did not get a new connection: %v", err)
	}
	if n := puts.Load(); n != 1 {
		t.Fatalf("%d writes arrived, want exactly 1", n)
	}
}

func TestEachClientHasItsOwnConnections(t *testing.T) {
	l, base, _ := server(t)
	a, _ := rest.HTTPClientFor(Config(base, 200*time.Millisecond))
	b, _ := rest.HTTPClientFor(Config(base, 200*time.Millisecond))
	for _, c := range []*http.Client{a, b} {
		if err := get(t, c, base.Host+"/api"); err != nil {
			t.Fatal(err)
		}
	}
	if n := l.accepted.Load(); n != 2 {
		t.Fatalf("%d connections for two clients, want one each", n)
	}
}

func TestAWatchIsRefusedNotCutShort(t *testing.T) {
	_, base, _ := server(t)
	c, _ := rest.HTTPClientFor(Config(base, 200*time.Millisecond))
	if err := get(t, c, base.Host+"/api/v1/pods?watch=true"); err == nil || !strings.Contains(err.Error(), "watch") {
		t.Fatalf("a watch through a bounded client: %v", err)
	}
}

func TestAnAnswerThatStartedIsReadToTheEnd(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		time.Sleep(400 * time.Millisecond) // longer than the attempt, shorter than the timeout
		_, _ = io.WriteString(w, "the rest")
	}))
	t.Cleanup(srv.Close)
	c, _ := rest.HTTPClientFor(Config(&rest.Config{Host: srv.URL, Timeout: 2 * time.Second}, 200*time.Millisecond))
	resp, err := c.Get(srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil || string(body) != "the rest" {
		t.Fatalf("%q %v: the attempt bound cut a body that was arriving", body, err)
	}
}
