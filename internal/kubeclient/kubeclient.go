// Package kubeclient makes a client-go configuration keep seeing the cluster while an API server
// dies with its machine.
//
// Behind the kubernetes Service, each connection is balanced to one API server. When that
// server's machine loses power its connections neither answer nor close: every request sent on
// one waits for the whole client timeout. With connections pooled, one observation after
// another lands on them. The lab measured the Cell Supervisor failing two observations in a
// row that way, forgetting what it had seen, and fencing at 9.7 s instead of ~4 s; Longhorn's
// managers, on HTTP/2, stayed blind for 44 s.
//
// So each attempt gets a short time to answer, and a failed attempt closes every connection the
// client holds -- what the kubelet does after a failed heartbeat -- so the next one is dialled and
// balanced afresh. Reads are retried at once within the caller's deadline; writes are not
// retried here, because only the caller knows whether its write is safe to repeat.
package kubeclient

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"sync"
	"time"

	utilnet "k8s.io/apimachinery/pkg/util/net"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/util/connrotation"
)

// MaxAttempts bounds the attempts of one read.
const MaxAttempts = 3

// Config returns a copy of base whose requests: use HTTP/1.1 (a timed-out request closes its
// connection, where one HTTP/2 connection would carry every request); must answer within
// attempt or be abandoned with every connection of this client closed; and, for reads, are tried
// again on a new connection, up to MaxAttempts and within base.Timeout. Each Config has its own
// connections: a failure in one client does not cut another's request short.
//
// It is for clients of short requests. A watch would be cut by the attempt bound, so watches are
// refused rather than silently broken.
func Config(base *rest.Config, attempt time.Duration) *rest.Config {
	return Balanced(base, attempt, nil)
}

// Balanced is Config with each connection to the Service dialled to an API server chosen by b,
// which avoids one that just failed an attempt. Without it a new connection still had a one in
// three chance of the dead machine's API server -- kube-proxy picks at random until the
// endpoint is removed, 15-30 s later -- and the supervisor still lost observations in a row
// (lab, chaos series 7). The request still names the Service, so TLS verifies the server
// against the Service's name as before: every API server's certificate carries it. With no API
// server known to b, connections go to the Service as with Config.
func Balanced(base *rest.Config, attempt time.Duration, b *Balancer) *rest.Config {
	cfg := rest.CopyConfig(base)
	plain := (&net.Dialer{Timeout: attempt, KeepAlive: 15 * time.Second}).DialContext
	dial := plain
	if b != nil {
		service := hostPort(cfg.Host)
		dial = func(ctx context.Context, network, addr string) (net.Conn, error) {
			target := addr
			if addr == service {
				if picked := b.pick(); picked != "" {
					target = picked
				}
			}
			c, err := plain(ctx, network, target)
			if err != nil && target != addr {
				b.fail(target)
			}
			return c, err
		}
	}
	dialer := connrotation.NewDialer(dial)
	cfg.Dial = dialer.DialContext
	cfg.TLSClientConfig.NextProtos = []string{"http/1.1"}
	inner := cfg.WrapTransport
	cfg.WrapTransport = func(rt http.RoundTripper) http.RoundTripper {
		if inner != nil {
			rt = inner(rt)
		}
		return &attempts{next: rt, attempt: attempt, closeAll: dialer.CloseAll, balancer: b}
	}
	return cfg
}

func hostPort(host string) string {
	u, err := url.Parse(host)
	if err != nil || u.Host == "" {
		return host
	}
	if u.Port() != "" {
		return u.Host
	}
	if u.Scheme == "http" {
		return net.JoinHostPort(u.Hostname(), "80")
	}
	return net.JoinHostPort(u.Hostname(), "443")
}

type attempts struct {
	next     http.RoundTripper
	attempt  time.Duration
	closeAll func()
	balancer *Balancer
}

// CloseIdleConnections passes the call on: without it, http.Client's would stop here and keep
// connections a caller asked to drop (client-go asks on a credential rotation, for one).
func (a *attempts) CloseIdleConnections() { utilnet.CloseIdleConnectionsFor(a.next) }

// WrappedRoundTripper is client-go's convention for reaching the transport underneath.
func (a *attempts) WrappedRoundTripper() http.RoundTripper { return a.next }

// ErrAttemptTimeout is the error of an attempt that had no answer in time.
var ErrAttemptTimeout = errors.New("no answer from the API server in time")

func (a *attempts) RoundTrip(req *http.Request) (*http.Response, error) {
	if isWatch(req.URL) {
		return nil, fmt.Errorf("kubeclient: %s is a watch; this client bounds every request to %s", req.URL.Path, a.attempt)
	}
	read := (req.Method == http.MethodGet || req.Method == http.MethodHead) && req.Body == nil
	var err error
	for i := 0; i < MaxAttempts; i++ {
		var resp *http.Response
		if resp, err = a.once(req); err == nil {
			return resp, nil
		}
		// Every connection goes, not only the one that failed: the others in the pool may lead
		// to the same dead machine, and each would cost a whole attempt to find out.
		a.closeAll()
		if !read || req.Context().Err() != nil {
			break
		}
	}
	return nil, err
}

// once sends one attempt. Its bound covers the wait for the response headers; the body is read
// under the caller's deadline only, so a large answer that has started arriving is not cut.
func (a *attempts) once(req *http.Request) (*http.Response, error) {
	ctx, cancel := context.WithCancel(req.Context())
	var mu sync.Mutex
	expired, remote := false, ""
	if a.balancer != nil {
		ctx = httptrace.WithClientTrace(ctx, &httptrace.ClientTrace{GotConn: func(info httptrace.GotConnInfo) {
			mu.Lock()
			remote = info.Conn.RemoteAddr().String()
			mu.Unlock()
		}})
	}
	timer := time.AfterFunc(a.attempt, func() {
		mu.Lock()
		expired = true
		mu.Unlock()
		cancel()
	})
	resp, err := a.next.RoundTrip(req.WithContext(ctx))
	timer.Stop()
	mu.Lock()
	timedOut, server := expired, remote
	mu.Unlock()
	if (err != nil || timedOut) && a.balancer != nil && server != "" {
		a.balancer.fail(server)
	}
	if err != nil {
		cancel()
		if timedOut {
			return nil, fmt.Errorf("%w (%s)", ErrAttemptTimeout, a.attempt)
		}
		return nil, err
	}
	if timedOut { // the answer came as the bound expired: its body is already cancelled
		resp.Body.Close()
		cancel()
		return nil, fmt.Errorf("%w (%s)", ErrAttemptTimeout, a.attempt)
	}
	resp.Body = &cancelOnClose{ReadCloser: resp.Body, cancel: cancel}
	return resp, nil
}

type cancelOnClose struct {
	io.ReadCloser
	cancel context.CancelFunc
}

func (c *cancelOnClose) Close() error {
	err := c.ReadCloser.Close()
	c.cancel()
	return err
}

func isWatch(u *url.URL) bool {
	v := u.Query().Get("watch")
	return v == "true" || v == "1"
}
