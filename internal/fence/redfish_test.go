package fence

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

const (
	sys  = "/redfish/v1/Systems/System.Embedded.1"
	user = "fence"
	pass = "s3cret-not-in-errors"
)

// bmc is a Redfish BMC with one system. ForceOff passes through PoweringOff for offPolls reads.
type bmc struct {
	mu       sync.Mutex
	state    string
	allowed  []string
	target   string
	offPolls int
	resets   []string
	redirect bool
}

func (b *bmc) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	u, p, ok := r.BasicAuth()
	if !ok || u != user || p != pass {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error":{"message":"Authentication required"}}`))
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.redirect {
		http.Redirect(w, r, "https://elsewhere.example/redfish/v1/Systems/1", http.StatusFound)
		return
	}
	switch {
	case r.Method == http.MethodGet && r.URL.Path == sys:
		if b.state == "PoweringOff" {
			if b.offPolls--; b.offPolls < 0 {
				b.state = "Off"
			}
		}
		target := b.target
		if target == "" {
			target = sys + "/Actions/ComputerSystem.Reset"
		}
		reset := map[string]any{"target": target}
		if b.allowed != nil {
			reset["ResetType@Redfish.AllowableValues"] = b.allowed
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"PowerState": b.state, "Actions": map[string]any{"#ComputerSystem.Reset": reset}})
	case r.Method == http.MethodPost && r.URL.Path == sys+"/Actions/ComputerSystem.Reset":
		var body struct{ ResetType string }
		if json.NewDecoder(r.Body).Decode(&body) != nil || r.Header.Get("Content-Type") != "application/json" {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		b.resets = append(b.resets, body.ResetType)
		switch body.ResetType {
		case "ForceOff":
			b.state = "PoweringOff"
		case "On":
			b.state = "On"
		}
		w.WriteHeader(http.StatusNoContent)
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

func (b *bmc) resetsSent() []string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]string(nil), b.resets...)
}

func newBMC(t *testing.T, b *bmc) (*httptest.Server, string) {
	srv := httptest.NewTLSServer(b)
	t.Cleanup(srv.Close)
	sum := sha256.Sum256(srv.Certificate().Raw)
	return srv, hex.EncodeToString(sum[:])
}

func pinned(t *testing.T, srv *httptest.Server, fp string) *Redfish {
	t.Helper()
	r, err := NewRedfish(srv.URL, user, pass, RedfishTLS{LeafSHA256: fp, RequestTimeout: 2 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func TestRedfishReadsPowerStateThroughAPinnedCertificate(t *testing.T) {
	srv, fp := newBMC(t, &bmc{state: "On"})
	r := pinned(t, srv, fp)
	if s, err := r.State(context.Background(), sys); err != nil || s != Running {
		t.Fatalf("%s %v", s, err)
	}
	// Fingerprints are accepted with colons and in upper case, as BMC UIs print them.
	var colons []string
	for i := 0; i < len(fp); i += 2 {
		colons = append(colons, strings.ToUpper(fp[i:i+2]))
	}
	if s, err := pinned(t, srv, strings.Join(colons, ":")).State(context.Background(), sys); err != nil || s != Running {
		t.Fatalf("%s %v", s, err)
	}
	// Pinned through a CA instead.
	ca := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw})
	viaCA, err := NewRedfish(srv.URL, user, pass, RedfishTLS{CAPEM: ca})
	if err != nil {
		t.Fatal(err)
	}
	if s, err := viaCA.State(context.Background(), sys); err != nil || s != Running {
		t.Fatalf("%s %v", s, err)
	}
}

func TestRedfishRefusesABMCWhoseCertificateIsNotThePinnedOne(t *testing.T) {
	srv, _ := newBMC(t, &bmc{state: "On"})
	other := strings.Repeat("ab", 32)
	s, err := pinned(t, srv, other).State(context.Background(), sys)
	if err == nil || s != Unknown || !strings.Contains(err.Error(), "not the pinned one") {
		t.Fatalf("%s %v", s, err)
	}
	wrongCA := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: mustOtherCert(t)})
	viaCA, err := NewRedfish(srv.URL, user, pass, RedfishTLS{CAPEM: wrongCA})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := viaCA.State(context.Background(), sys); err == nil {
		t.Fatal("accepted a certificate the pinned CA did not sign")
	}
}

// mustOtherCert is a self-signed CA that signed nothing the test BMC presents.
func mustOtherCert(t *testing.T) []byte {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "other CA"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), IsCA: true,
		BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	return der
}

func TestRedfishConfigurationIsFailClosed(t *testing.T) {
	fp := strings.Repeat("ab", 32)
	for name, c := range map[string]struct {
		endpoint, user, pass string
		pin                  RedfishTLS
	}{
		"plain http":           {"http://10.0.0.1", user, pass, RedfishTLS{LeafSHA256: fp}},
		"no pin":               {"https://10.0.0.1", user, pass, RedfishTLS{}},
		"two pins":             {"https://10.0.0.1", user, pass, RedfishTLS{LeafSHA256: fp, CAPEM: []byte("x")}},
		"short fingerprint":    {"https://10.0.0.1", user, pass, RedfishTLS{LeafSHA256: "abcd"}},
		"credentials in url":   {"https://u:p@10.0.0.1", user, pass, RedfishTLS{LeafSHA256: fp}},
		"path in endpoint":     {"https://10.0.0.1/redfish", user, pass, RedfishTLS{LeafSHA256: fp}},
		"no password":          {"https://10.0.0.1", user, "", RedfishTLS{LeafSHA256: fp}},
		"CA that is not a PEM": {"https://10.0.0.1", user, pass, RedfishTLS{CAPEM: []byte("not pem")}},
	} {
		if _, err := NewRedfish(c.endpoint, c.user, c.pass, c.pin); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestRedfishForceOffIsConfirmedOnlyWhenTheBMCSaysOff(t *testing.T) {
	b := &bmc{state: "On", offPolls: 2, allowed: []string{"On", "ForceOff", "GracefulShutdown"}}
	srv, fp := newBMC(t, b)
	r := pinned(t, srv, fp)
	powered, err := EnsureOff(context.Background(), r, sys, 5*time.Second)
	if err != nil || !powered {
		t.Fatalf("powered=%v err=%v", powered, err)
	}
	if got := b.resetsSent(); len(got) != 1 || got[0] != "ForceOff" {
		t.Fatalf("resets %v", got)
	}
	b.mu.Lock()
	final := b.state
	b.mu.Unlock()
	if final != "Off" {
		t.Fatalf("confirmed off while the BMC still said %s", final)
	}
	if err := r.PowerOn(context.Background(), sys); err != nil {
		t.Fatal(err)
	}
	if s, _ := r.State(context.Background(), sys); s != Running {
		t.Fatal(s)
	}
}

func TestRedfishNeverFallsBackToAGracefulShutdown(t *testing.T) {
	b := &bmc{state: "On", allowed: []string{"On", "GracefulShutdown"}}
	srv, fp := newBMC(t, b)
	err := pinned(t, srv, fp).PowerOff(context.Background(), sys)
	if err == nil || !strings.Contains(err.Error(), "does not allow ResetType ForceOff") {
		t.Fatal(err)
	}
	if len(b.resetsSent()) != 0 {
		t.Fatalf("sent %v", b.resetsSent())
	}
}

func TestRedfishUnexpectedStatesAndFailuresAreUnknown(t *testing.T) {
	b := &bmc{state: "Rebooting"}
	srv, fp := newBMC(t, b)
	r := pinned(t, srv, fp)
	if s, err := r.State(context.Background(), sys); s != Unknown || err == nil {
		t.Fatalf("%s %v", s, err)
	}
	wrong, _ := NewRedfish(srv.URL, user, "wrong-password-value", RedfishTLS{LeafSHA256: fp})
	s, err := wrong.State(context.Background(), sys)
	if s != Unknown || err == nil || !strings.Contains(err.Error(), "401") {
		t.Fatalf("%s %v", s, err)
	}
	if strings.Contains(err.Error(), "wrong-password-value") || strings.Contains(err.Error(), pass) {
		t.Fatal("an error carries the password")
	}
	b.mu.Lock()
	b.redirect = true
	b.mu.Unlock()
	if s, err := r.State(context.Background(), sys); s != Unknown || err == nil || !strings.Contains(err.Error(), "redirect") {
		t.Fatalf("followed a redirect: %s %v", s, err)
	}
}

func TestRedfishRefusesPathsAndTargetsOutsideTheSystem(t *testing.T) {
	b := &bmc{state: "On", target: "/redfish/v1/Managers/1/Actions/Manager.Reset"}
	srv, fp := newBMC(t, b)
	r := pinned(t, srv, fp)
	for _, p := range []string{"/redfish/v1/Systems/../Managers/1", "/redfish/v1/Managers/1", "/redfish/v1/Systems/1?x=1"} {
		if _, err := r.State(context.Background(), p); err == nil || !strings.Contains(err.Error(), "refusing") {
			t.Fatalf("%s: %v", p, err)
		}
	}
	if err := r.PowerOff(context.Background(), sys); err == nil || !strings.Contains(err.Error(), "outside the system") {
		t.Fatal(err)
	}
	if len(b.resetsSent()) != 0 {
		t.Fatal("a reset reached the BMC")
	}
}

type named struct{ name string }

func (n named) Name() string { return n.name }
func (n named) State(_ context.Context, id string) (State, error) {
	if id == "off-one" {
		return Off, nil
	}
	return Running, nil
}
func (named) PowerOff(context.Context, string) error { return nil }
func (named) PowerOn(context.Context, string) error  { return nil }

func TestRouterSendsEachMachineToItsOwnController(t *testing.T) {
	r, err := NewRouter(map[string]Target{
		"node-1": {Fencer: named{"redfish:https://bmc-1"}, ID: "off-one"},
		"node-2": {Fencer: named{"redfish:https://bmc-2"}, ID: "/redfish/v1/Systems/1"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if s, _ := r.State(context.Background(), "node-1"); s != Off {
		t.Fatal(s)
	}
	if s, _ := r.State(context.Background(), "node-2"); s != Running {
		t.Fatal(s)
	}
	if s, err := r.State(context.Background(), "node-9"); s != Unknown || err == nil {
		t.Fatalf("%s %v", s, err)
	}
	if err := r.PowerOff(context.Background(), "node-9"); err == nil {
		t.Fatal("powered off an unconfigured machine")
	}
	_, err = NewRouter(map[string]Target{
		"node-1": {Fencer: named{"redfish:https://bmc-1"}, ID: "/redfish/v1/Systems/1"},
		"node-2": {Fencer: named{"redfish:https://bmc-1"}, ID: "/redfish/v1/Systems/1"},
	})
	if err == nil {
		t.Fatal("two nodes on one power target accepted")
	}
}
