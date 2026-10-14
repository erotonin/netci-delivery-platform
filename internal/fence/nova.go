package fence

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
)

// Nova fences OpenStack servers through the Compute API, authenticated by a Keystone application
// credential that may do nothing but read, stop and start the controllers' servers.
//
// Off is what Nova reports once the hypervisor has stopped the server: status SHUTOFF with power
// state SHUTDOWN (4). Anything else -- ERROR, a task in progress, a power state Nova has not
// refreshed -- is Unknown, and Unknown is never Off. A server whose hypervisor host is itself down
// cannot be stopped, and Nova keeps reporting what it last knew: it stays Unknown or Running and
// is never fenced, which stalls a takeover rather than risk two writers. An operator then marks the
// compute service down (forced down), after which Nova can be told the truth.
//
// os-stop asks the hypervisor to stop the server: libvirt waits up to the server's shutdown timeout
// for a clean stop before it destroys it. For a fence that wait is the time the old controller may
// still write; give the controllers' image os_shutdown_timeout=0 so the stop is immediate.
type Nova struct {
	AuthURL    string // Keystone, e.g. https://keystone.example:5000/v3
	Region     string
	Interface  string // "internal" (default) or "public"
	CredID     string
	CredSecret string
	Client     *http.Client

	mu      sync.Mutex
	token   string
	expires time.Time
	compute string
}

// NewNova builds the client; caPEM, when set, is the only CA trusted for Keystone and Nova.
func NewNova(authURL, region, iface, credID, credSecret string, caPEM []byte, timeout time.Duration) (*Nova, error) {
	u, err := url.Parse(authURL)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" {
		return nil, fmt.Errorf("nova authURL %q must be an http(s) URL", authURL)
	}
	if credID == "" || credSecret == "" {
		return nil, errors.New("nova needs an application credential id and secret")
	}
	if iface == "" {
		iface = "internal"
	}
	if iface != "internal" && iface != "public" && iface != "admin" {
		return nil, fmt.Errorf("nova interface %q: internal, public or admin", iface)
	}
	tr := http.DefaultTransport.(*http.Transport).Clone()
	if len(caPEM) > 0 {
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caPEM) {
			return nil, errors.New("nova ca.crt: no certificate in it")
		}
		tr.TLSClientConfig = &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
	}
	// A fresh connection per request: a pooled one could sit on an API endpoint that went away with
	// the failure being handled (the plugin's Kubernetes fence learned that, ADR-079).
	tr.DisableKeepAlives = true
	return &Nova{AuthURL: strings.TrimSuffix(authURL, "/"), Region: region, Interface: iface, CredID: credID, CredSecret: credSecret,
		Client: &http.Client{Transport: tr, Timeout: timeout}}, nil
}

func (n *Nova) Name() string { return "nova:" + n.AuthURL }

var serverID = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// login gets a token and the compute endpoint from Keystone, reusing them until a minute before
// the token expires.
func (n *Nova) login(ctx context.Context) (string, string, error) {
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.token != "" && time.Now().Before(n.expires.Add(-time.Minute)) {
		return n.token, n.compute, nil
	}
	body, _ := json.Marshal(map[string]any{"auth": map[string]any{"identity": map[string]any{
		"methods":                []string{"application_credential"},
		"application_credential": map[string]string{"id": n.CredID, "secret": n.CredSecret},
	}}})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, n.AuthURL+"/auth/tokens", bytes.NewReader(body))
	if err != nil {
		return "", "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := n.Client.Do(req)
	if err != nil {
		return "", "", fmt.Errorf("keystone: %w", err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusCreated {
		// Keystone's own message only: never the request, which carries the secret.
		return "", "", &NovaError{Keystone: true, Status: resp.StatusCode, Message: novaMessage(data)}
	}
	var tok struct {
		Token struct {
			ExpiresAt time.Time `json:"expires_at"`
			Catalog   []struct {
				Type      string `json:"type"`
				Endpoints []struct {
					Interface string `json:"interface"`
					Region    string `json:"region"`
					URL       string `json:"url"`
				} `json:"endpoints"`
			} `json:"catalog"`
		} `json:"token"`
	}
	if err := json.Unmarshal(data, &tok); err != nil {
		return "", "", fmt.Errorf("keystone: unreadable token: %w", err)
	}
	subject := resp.Header.Get("X-Subject-Token")
	if subject == "" {
		return "", "", errors.New("keystone: no X-Subject-Token")
	}
	compute := ""
	for _, svc := range tok.Token.Catalog {
		if svc.Type != "compute" {
			continue
		}
		for _, ep := range svc.Endpoints {
			if ep.Interface == n.Interface && (n.Region == "" || ep.Region == n.Region) {
				compute = strings.TrimSuffix(ep.URL, "/")
			}
		}
	}
	if compute == "" {
		return "", "", fmt.Errorf("keystone: no %s compute endpoint in region %q", n.Interface, n.Region)
	}
	n.token, n.expires, n.compute = subject, tok.Token.ExpiresAt, compute
	return n.token, n.compute, nil
}

func (n *Nova) do(ctx context.Context, method, path string, body any, out any) error {
	token, compute, err := n.login(ctx)
	if err != nil {
		return err
	}
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, compute+path, rdr)
	if err != nil {
		return err
	}
	req.Header.Set("X-Auth-Token", token)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	resp, err := n.Client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode == http.StatusUnauthorized {
		n.mu.Lock()
		n.token = "" // expired early, or revoked: the next call logs in again
		n.mu.Unlock()
	}
	if resp.StatusCode >= 300 {
		return &NovaError{Method: method, Path: path, Status: resp.StatusCode, Message: novaMessage(data)}
	}
	if out != nil {
		return json.Unmarshal(data, out)
	}
	return nil
}

func (n *Nova) State(ctx context.Context, id string) (State, error) {
	if !serverID.MatchString(id) {
		return Unknown, fmt.Errorf("nova: %q is not a server id", id)
	}
	var s struct {
		Server struct {
			Status     string  `json:"status"`
			PowerState *int    `json:"OS-EXT-STS:power_state"`
			TaskState  *string `json:"OS-EXT-STS:task_state"`
		} `json:"server"`
	}
	if err := n.do(ctx, http.MethodGet, "/servers/"+id, nil, &s); err != nil {
		return Unknown, err
	}
	busy := s.Server.TaskState != nil && *s.Server.TaskState != ""
	switch {
	case s.Server.Status == "SHUTOFF" && s.Server.PowerState != nil && *s.Server.PowerState == 4 && !busy:
		return Off, nil
	case s.Server.Status == "ACTIVE" && s.Server.PowerState != nil && *s.Server.PowerState == 1:
		return Running, nil
	default:
		return Unknown, nil
	}
}

func (n *Nova) PowerOff(ctx context.Context, id string) error {
	if !serverID.MatchString(id) {
		return fmt.Errorf("nova: %q is not a server id", id)
	}
	return n.do(ctx, http.MethodPost, "/servers/"+id+"/action", map[string]any{"os-stop": nil}, nil)
}

func (n *Nova) PowerOn(ctx context.Context, id string) error {
	if !serverID.MatchString(id) {
		return fmt.Errorf("nova: %q is not a server id", id)
	}
	return n.do(ctx, http.MethodPost, "/servers/"+id+"/action", map[string]any{"os-start": nil}, nil)
}

// NovaError is Keystone's or Nova's non-2xx answer.
type NovaError struct {
	Keystone     bool
	Method, Path string
	Status       int
	Message      string
}

func (e *NovaError) Error() string {
	if e.Keystone {
		return fmt.Sprintf("keystone refused the application credential: HTTP %d: %s", e.Status, e.Message)
	}
	return fmt.Sprintf("nova %s %s: HTTP %d: %s", e.Method, e.Path, e.Status, e.Message)
}

// Refused: wrong or not allowed, and asking again will not change it. Keystone's 401 is a bad
// credential; Nova's 401 is a token that expired early, and the next call logs in again. Nova's 409
// is a server busy with another task (or already stopped), never a refusal.
func (e *NovaError) Refused() bool {
	switch e.Status {
	case http.StatusUnauthorized:
		return e.Keystone
	case http.StatusBadRequest, http.StatusForbidden, http.StatusNotFound:
		return true
	}
	return false
}

// novaMessage: the error's text from an OpenStack error body ({"<kind>": {"message": ...}}).
func novaMessage(data []byte) string {
	var m map[string]struct {
		Message string `json:"message"`
	}
	if json.Unmarshal(data, &m) == nil {
		for kind, v := range m {
			if v.Message != "" {
				return kind + ": " + v.Message
			}
		}
	}
	s := strings.TrimSpace(string(data))
	if len(s) > 200 {
		s = s[:200]
	}
	return s
}
