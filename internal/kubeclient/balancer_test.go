package kubeclient

import (
	"context"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	discoveryv1 "k8s.io/api/discovery/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
	"k8s.io/client-go/rest"
)

func TestTheBalancerTakesTurnsAndAvoidsAFailedServer(t *testing.T) {
	now := time.Unix(1000, 0)
	b := NewBalancer(20 * time.Second)
	b.Now = func() time.Time { return now }
	b.Set([]string{"10.0.0.2:6443", "10.0.0.1:6443", "10.0.0.3:6443", "10.0.0.1:6443"})
	if got := b.Addrs(); len(got) != 3 {
		t.Fatalf("%v", got)
	}
	seen := map[string]int{}
	for i := 0; i < 6; i++ {
		seen[b.pick()]++
	}
	if len(seen) != 3 || seen["10.0.0.1:6443"] != 2 {
		t.Fatalf("not in turn: %v", seen)
	}
	b.fail("10.0.0.2:6443")
	for i := 0; i < 6; i++ {
		if b.pick() == "10.0.0.2:6443" {
			t.Fatal("a server that just failed was chosen again")
		}
	}
	now = now.Add(21 * time.Second)
	seen = map[string]int{}
	for i := 0; i < 3; i++ {
		seen[b.pick()]++
	}
	if seen["10.0.0.2:6443"] != 1 {
		t.Fatalf("a server was avoided past its time: %v", seen)
	}
}

func TestWhenEveryServerFailedTheOneAvoidedLongestIsTried(t *testing.T) {
	now := time.Unix(1000, 0)
	b := NewBalancer(20 * time.Second)
	b.Now = func() time.Time { return now }
	b.Set([]string{"a:1", "b:1"})
	b.fail("b:1")
	now = now.Add(5 * time.Second)
	b.fail("a:1")
	if got := b.pick(); got != "b:1" {
		t.Fatalf("picked %q, want the one whose avoidance ends first", got)
	}
	b.Set(nil)
	if len(b.Addrs()) != 2 {
		t.Fatal("an empty list replaced the known servers")
	}
}

// The machine of one API server lost its power: its address accepts nothing and answers
// nothing. Reads through the Service name go to the live server, and the dead one costs one
// attempt, not one per request.
func TestReadsAvoidTheAPIServerOfALostMachine(t *testing.T) {
	dead := &deadable{}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	dead.Listener = ln
	go func() { // accepts, then is silent: what a client sees of a machine without power
		for {
			c, err := dead.Accept()
			if err != nil {
				return
			}
			c.(*deadConn).dead.Store(true)
		}
	}()
	defer ln.Close()
	live := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok") }))
	defer live.Close()

	b := NewBalancer(20 * time.Second)
	b.Set([]string{ln.Addr().String(), live.Listener.Addr().String()})
	cfg := Balanced(&rest.Config{Host: "http://kubernetes.default.svc.invalid", Timeout: 2 * time.Second}, 200*time.Millisecond, b)
	c, err := rest.HTTPClientFor(cfg)
	if err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	for i := 0; i < 6; i++ {
		c.CloseIdleConnections() // each read dials, as after a failure dropped the pool
		if err := get(t, c, "http://kubernetes.default.svc.invalid/api"); err != nil {
			t.Fatalf("read %d: %v", i, err)
		}
	}
	if took := time.Since(start); took > time.Second {
		t.Fatalf("6 reads took %s: the dead server cost more than one attempt", took)
	}
	if n := dead.accepted.Load(); n > 1 {
		t.Fatalf("the dead server was dialled %d times", n)
	}
}

func TestAServerThatRefusesConnectionsIsAvoided(t *testing.T) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	refused := ln.Addr().String()
	ln.Close()
	live := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {}))
	defer live.Close()
	b := NewBalancer(20 * time.Second)
	b.Set([]string{refused, live.Listener.Addr().String()})
	c, _ := rest.HTTPClientFor(Balanced(&rest.Config{Host: "http://svc.invalid", Timeout: 2 * time.Second}, 200*time.Millisecond, b))
	for i := 0; i < 4; i++ {
		c.CloseIdleConnections() // each read dials: both servers are tried
		if err := get(t, c, "http://svc.invalid/api"); err != nil {
			t.Fatal(err)
		}
	}
	if b.pick() == refused || b.pick() == refused {
		t.Fatal("a server that refused the connection was not avoided")
	}
}

func TestAPIServersAreDiscoveredFromTheServiceEndpoints(t *testing.T) {
	port, name, no := int32(6443), "https", false
	client := fake.NewSimpleClientset(&discoveryv1.EndpointSlice{
		ObjectMeta: metav1.ObjectMeta{Name: "kubernetes", Namespace: "default", Labels: map[string]string{discoveryv1.LabelServiceName: "kubernetes"}},
		Ports:      []discoveryv1.EndpointPort{{Name: &name, Port: &port}},
		Endpoints: []discoveryv1.Endpoint{{Addresses: []string{"192.168.122.211"}}, {Addresses: []string{"192.168.122.212"}},
			{Addresses: []string{"192.168.122.213"}, Conditions: discoveryv1.EndpointConditions{Ready: &no}}},
	})
	b := NewBalancer(time.Second)
	ctx, cancel := context.WithCancel(context.Background())
	go Discover(ctx, client, b, time.Hour, slog.New(slog.NewTextHandler(io.Discard, nil)))
	deadline := time.Now().Add(2 * time.Second)
	for len(b.Addrs()) == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	got := b.Addrs()
	if len(got) != 2 || got[0] != "192.168.122.211:6443" || got[1] != "192.168.122.212:6443" {
		t.Fatalf("%v: want the two ready API servers", got)
	}
}
