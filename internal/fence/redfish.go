package fence

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Redfish fences machines through their BMC (iDRAC, iLO, XClarity, OpenBMC, ...) with the
// DMTF Redfish API: the machine argument is the ComputerSystem's path, such as
// /redfish/v1/Systems/System.Embedded.1.
//
// Fail-closed choices:
//   - TLS is always verified, against a pinned CA or a pinned leaf-certificate fingerprint
//     (BMCs mostly carry self-signed certificates); there is no way to switch verification off;
//   - power-off is ForceOff only: a GracefulShutdown leaves the machine writing for as long as
//     its OS takes, which is exactly what fencing must not allow, so a BMC that does not offer
//     ForceOff is an error, never a fallback;
//   - a PowerState other than On/Off (and the transitions to them) is Unknown;
//   - redirects are refused, and credentials never appear in an error or a log.
type Redfish struct {
	Endpoint string // https://host[:port]
	Username string
	Password string
	Client   *http.Client
}

// RedfishTLS pins the BMC's identity: a CA (PEM) or the SHA-256 of its leaf certificate (hex,
// colons allowed). Exactly one is required.
type RedfishTLS struct {
	CAPEM          []byte
	LeafSHA256     string
	ServerName     string // optional; defaults to the endpoint's host when a CA is pinned
	RequestTimeout time.Duration
}

// NewRedfish validates the endpoint and builds a client whose TLS is pinned.
func NewRedfish(endpoint, username, password string, pin RedfishTLS) (*Redfish, error) {
	u, err := url.Parse(endpoint)
	if err != nil || u.Scheme != "https" || u.Host == "" || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.User != nil {
		return nil, fmt.Errorf("redfish endpoint must be https://host[:port], got %q", endpoint)
	}
	if username == "" || password == "" {
		return nil, errors.New("redfish needs a username and a password")
	}
	tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12}
	switch {
	case len(pin.CAPEM) > 0 && pin.LeafSHA256 != "":
		return nil, errors.New("pin either a CA or a leaf fingerprint, not both")
	case len(pin.CAPEM) > 0:
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pin.CAPEM) {
			return nil, errors.New("the pinned CA is not a PEM certificate")
		}
		tlsCfg.RootCAs = pool
		tlsCfg.ServerName = pin.ServerName
	case pin.LeafSHA256 != "":
		want, err := hex.DecodeString(strings.ReplaceAll(strings.ToLower(pin.LeafSHA256), ":", ""))
		if err != nil || len(want) != sha256.Size {
			return nil, errors.New("the pinned fingerprint must be a SHA-256 in hex")
		}
		// The chain is not checked against any CA: the leaf itself is what is trusted. Go then
		// requires InsecureSkipVerify for its own chain check to be replaced, which this
		// callback does in full -- a connection whose leaf differs is refused.
		tlsCfg.InsecureSkipVerify = true
		tlsCfg.VerifyPeerCertificate = func(raw [][]byte, _ [][]*x509.Certificate) error {
			if len(raw) == 0 {
				return errors.New("redfish: the BMC presented no certificate")
			}
			got := sha256.Sum256(raw[0])
			if !bytes.Equal(got[:], want) {
				return fmt.Errorf("redfish: the BMC's certificate is not the pinned one (sha256 %x)", got)
			}
			return nil
		}
	default:
		return nil, errors.New("redfish needs a pinned CA or a pinned certificate fingerprint")
	}
	timeout := pin.RequestTimeout
	if timeout == 0 {
		timeout = 5 * time.Second
	}
	return &Redfish{
		Endpoint: strings.TrimSuffix(endpoint, "/"), Username: username, Password: password,
		Client: &http.Client{
			Timeout:   timeout,
			Transport: &http.Transport{TLSClientConfig: tlsCfg, Proxy: nil, MaxIdleConnsPerHost: 2, IdleConnTimeout: 30 * time.Second},
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return errors.New("redfish: refusing a redirect") // credentials would follow it
			},
		},
	}, nil
}

func (r *Redfish) Name() string { return "redfish:" + r.Endpoint }

type computerSystem struct {
	PowerState string `json:"PowerState"`
	Actions    struct {
		Reset struct {
			Target  string   `json:"target"`
			Allowed []string `json:"ResetType@Redfish.AllowableValues"`
		} `json:"#ComputerSystem.Reset"`
	} `json:"Actions"`
}

func (r *Redfish) system(ctx context.Context, path string) (*computerSystem, error) {
	if err := checkSystemPath(path); err != nil {
		return nil, err
	}
	var cs computerSystem
	if err := r.do(ctx, http.MethodGet, path, nil, &cs); err != nil {
		return nil, err
	}
	return &cs, nil
}

// State maps Redfish's PowerState. PoweringOff is Running: until the BMC says Off, the machine
// may still be writing.
func (r *Redfish) State(ctx context.Context, path string) (State, error) {
	cs, err := r.system(ctx, path)
	if err != nil {
		return Unknown, err
	}
	switch cs.PowerState {
	case "Off":
		return Off, nil
	case "On", "PoweringOn", "PoweringOff", "Paused":
		return Running, nil
	default:
		return Unknown, fmt.Errorf("redfish: unexpected PowerState %q", cs.PowerState)
	}
}

// PowerOff sends ForceOff.
func (r *Redfish) PowerOff(ctx context.Context, path string) error {
	return r.reset(ctx, path, "ForceOff")
}

// PowerOn sends On.
func (r *Redfish) PowerOn(ctx context.Context, path string) error { return r.reset(ctx, path, "On") }

func (r *Redfish) reset(ctx context.Context, path, kind string) error {
	cs, err := r.system(ctx, path)
	if err != nil {
		return err
	}
	target := cs.Actions.Reset.Target
	if target == "" {
		target = path + "/Actions/ComputerSystem.Reset"
	}
	if err := checkSystemPath(target); err != nil || !strings.HasPrefix(target, path+"/") {
		return fmt.Errorf("redfish: the BMC names a reset target outside the system: %q", target)
	}
	if len(cs.Actions.Reset.Allowed) > 0 && !contains(cs.Actions.Reset.Allowed, kind) {
		return fmt.Errorf("redfish: the BMC does not allow ResetType %s (allows %v)", kind, cs.Actions.Reset.Allowed)
	}
	return r.do(ctx, http.MethodPost, target, map[string]string{"ResetType": kind}, nil)
}

func (r *Redfish) do(ctx context.Context, method, path string, body any, out any) error {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, r.Endpoint+path, rd)
	if err != nil {
		return err
	}
	req.SetBasicAuth(r.Username, r.Password)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := r.Client.Do(req)
	if err != nil {
		var ue *url.Error
		if errors.As(err, &ue) {
			return fmt.Errorf("redfish %s %s: %w", method, path, ue.Err) // the URL never carries credentials, but keep the message short
		}
		return err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return &HTTPError{Method: method, Path: path, Status: resp.StatusCode, Message: redfishMessage(data)}
	}
	if out == nil {
		return nil
	}
	if err := json.Unmarshal(data, out); err != nil {
		return fmt.Errorf("redfish %s %s: %w", method, path, err)
	}
	return nil
}

// HTTPError is a BMC's non-2xx answer.
type HTTPError struct {
	Method, Path string
	Status       int
	Message      string
}

func (e *HTTPError) Error() string {
	return fmt.Sprintf("redfish %s %s: HTTP %d%s", e.Method, e.Path, e.Status, e.Message)
}

// Refused: the request itself is wrong or not allowed. 409 is not a refusal: iDRAC answers it
// for "already off" and while it settles after a power change.
func (e *HTTPError) Refused() bool {
	switch e.Status {
	case http.StatusBadRequest, http.StatusUnauthorized, http.StatusForbidden, http.StatusNotFound, http.StatusMethodNotAllowed:
		return true
	}
	return false
}

// redfishMessage extracts the standard error message, if any, without echoing the whole body.
// iDRAC puts it under error.@Message.ExtendedInfo; that is used when error.message is empty.
func redfishMessage(data []byte) string {
	var e struct {
		Error struct {
			Message  string `json:"message"`
			Extended []struct {
				Message string `json:"Message"`
			} `json:"@Message.ExtendedInfo"`
		} `json:"error"`
	}
	if json.Unmarshal(data, &e) == nil && e.Error.Message == "" && len(e.Error.Extended) > 0 {
		e.Error.Message = e.Error.Extended[0].Message
	}
	if e.Error.Message != "" {
		msg := e.Error.Message
		if len(msg) > 200 {
			msg = msg[:200]
		}
		return ": " + msg
	}
	return ""
}

func checkSystemPath(p string) error {
	if !strings.HasPrefix(p, "/redfish/v1/Systems/") || strings.Contains(p, "..") || strings.ContainsAny(p, "?#\\ \t\r\n") {
		return fmt.Errorf("redfish: refusing system path %q", p)
	}
	return nil
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}
