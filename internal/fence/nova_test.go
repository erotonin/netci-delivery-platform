package fence

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

const (
	novaServer = "6f1b6a8e-3f2c-4c7e-9d2a-0a1b2c3d4e5f"
	novaSecret = "the-application-credential-secret"
)

// fakeCloud is Keystone and Nova for one server, as an OpenStack cloud answers them.
type fakeCloud struct {
	mu          sync.Mutex
	srv         *httptest.Server
	logins      int
	status      string
	power       int
	task        string
	stopsLeft   int // polls after an os-stop before the server reports SHUTOFF
	stopAnswer  int // the os-stop's answer (202 when 0)
	expireToken bool
	stops       int
}

func newFakeCloud(t *testing.T) *fakeCloud {
	f := &fakeCloud{status: "ACTIVE", power: 1}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v3/auth/tokens", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Auth struct {
				Identity struct {
					Methods []string                    `json:"methods"`
					Cred    struct{ ID, Secret string } `json:"application_credential"`
				} `json:"identity"`
			} `json:"auth"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		f.mu.Lock()
		defer f.mu.Unlock()
		if body.Auth.Identity.Cred.ID != "cred-id" || body.Auth.Identity.Cred.Secret != novaSecret {
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte(`{"error": {"message": "The request you have made requires authentication."}}`))
			return
		}
		f.logins++
		w.Header().Set("X-Subject-Token", "token-"+string(rune('a'+f.logins)))
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{"token": map[string]any{
			"expires_at": time.Now().Add(time.Hour).UTC().Format(time.RFC3339),
			"catalog": []any{
				map[string]any{"type": "identity", "endpoints": []any{map[string]any{"interface": "internal", "region": "RegionOne", "url": f.srv.URL + "/v3"}}},
				map[string]any{"type": "compute", "endpoints": []any{
					map[string]any{"interface": "public", "region": "RegionOne", "url": "https://public.invalid/compute"},
					map[string]any{"interface": "internal", "region": "RegionTwo", "url": "https://other.invalid/compute"},
					map[string]any{"interface": "internal", "region": "RegionOne", "url": f.srv.URL + "/compute/v2.1"},
				}},
			}}})
	})
	authed := func(w http.ResponseWriter, r *http.Request) bool {
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.expireToken {
			f.expireToken = false
			w.WriteHeader(http.StatusUnauthorized)
			return false
		}
		return strings.HasPrefix(r.Header.Get("X-Auth-Token"), "token-")
	}
	mux.HandleFunc("GET /compute/v2.1/servers/{id}", func(w http.ResponseWriter, r *http.Request) {
		if !authed(w, r) {
			return
		}
		if r.PathValue("id") != novaServer {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"itemNotFound": {"message": "Instance could not be found."}}`))
			return
		}
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.stopsLeft > 0 {
			f.stopsLeft--
			if f.stopsLeft == 0 {
				f.status, f.power, f.task = "SHUTOFF", 4, ""
			}
		}
		var task any
		if f.task != "" {
			task = f.task
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"server": map[string]any{
			"id": novaServer, "status": f.status, "OS-EXT-STS:power_state": f.power, "OS-EXT-STS:task_state": task}})
	})
	mux.HandleFunc("POST /compute/v2.1/servers/{id}/action", func(w http.ResponseWriter, r *http.Request) {
		if !authed(w, r) {
			return
		}
		var action map[string]any
		_ = json.NewDecoder(r.Body).Decode(&action)
		f.mu.Lock()
		defer f.mu.Unlock()
		if _, ok := action["os-stop"]; ok {
			f.stops++
			if f.stopAnswer != 0 {
				w.WriteHeader(f.stopAnswer)
				_, _ = w.Write([]byte(`{"conflictingRequest": {"message": "Cannot 'stop' instance while it is in task_state powering-off"}}`))
				return
			}
			f.task, f.stopsLeft = "powering-off", 2
		}
		w.WriteHeader(http.StatusAccepted)
	})
	f.srv = httptest.NewServer(mux)
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeCloud) nova(t *testing.T, secret string) *Nova {
	n, err := NewNova(f.srv.URL+"/v3", "RegionOne", "", "cred-id", secret, nil, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	return n
}

func TestNovaFencesAServerThroughKeystoneAndTheComputeAPI(t *testing.T) {
	f := newFakeCloud(t)
	n := f.nova(t, novaSecret)
	ctx := context.Background()
	if st, err := n.State(ctx, novaServer); err != nil || st != Running {
		t.Fatalf("an active server: %v %v", st, err)
	}
	powered, err := EnsureOff(ctx, n, novaServer, 10*time.Second)
	if err != nil || !powered {
		t.Fatalf("fence: %v %v", powered, err)
	}
	if st, _ := n.State(ctx, novaServer); st != Off {
		t.Fatalf("after the fence: %v", st)
	}
	if f.logins != 1 {
		t.Fatalf("logged in %d times: the token is reused until it nears its expiry", f.logins)
	}
	if powered, err := EnsureOff(ctx, n, novaServer, time.Second); err != nil || powered {
		t.Fatalf("a server already off is not powered off again: %v %v", powered, err)
	}
}

// Off is only SHUTOFF with power state SHUTDOWN and no task in progress; anything in between, or
// a state Nova has not refreshed, is Unknown -- which never counts as fenced.
func TestNovaSaysOffOnlyWhenTheHypervisorHasStoppedTheServer(t *testing.T) {
	f := newFakeCloud(t)
	n := f.nova(t, novaSecret)
	ctx := context.Background()
	for _, c := range []struct {
		status string
		power  int
		task   string
		want   State
	}{
		{"SHUTOFF", 4, "", Off},
		{"SHUTOFF", 4, "powering-on", Unknown},
		{"ACTIVE", 1, "powering-off", Running},
		{"ACTIVE", 4, "", Unknown}, // the hypervisor's view not caught up
		{"ERROR", 0, "", Unknown},
		{"SHUTOFF", 1, "", Unknown},
	} {
		f.mu.Lock()
		f.status, f.power, f.task = c.status, c.power, c.task
		f.mu.Unlock()
		if got, err := n.State(ctx, novaServer); err != nil || got != c.want {
			t.Errorf("%s/%d/%q: %v %v, want %v", c.status, c.power, c.task, got, err, c.want)
		}
	}
}

func TestNovasAnswersAreRefusalsOnlyWhenAskingAgainCannotHelp(t *testing.T) {
	f := newFakeCloud(t)
	ctx := context.Background()
	var r Refusal
	// A wrong credential: Keystone's 401 is final, and the secret is in no error.
	_, err := f.nova(t, "wrong-secret").State(ctx, novaServer)
	if !errors.As(err, &r) || !r.Refused() || strings.Contains(err.Error(), "wrong-secret") {
		t.Fatalf("a wrong credential: %v", err)
	}
	n := f.nova(t, novaSecret)
	// A token Nova no longer takes: not a refusal, and the next call logs in again.
	f.expireToken = true
	if _, err := n.State(ctx, novaServer); !errors.As(err, &r) || r.Refused() {
		t.Fatalf("an expired token: %v", err)
	}
	if st, err := n.State(ctx, novaServer); err != nil || st != Running || f.logins != 2 {
		t.Fatalf("after the expired token: %v %v, %d logins", st, err, f.logins)
	}
	// A server busy with another task: 409, asked again.
	f.stopAnswer = http.StatusConflict
	if err := n.PowerOff(ctx, novaServer); !errors.As(err, &r) || r.Refused() {
		t.Fatalf("a busy server: %v", err)
	}
	// A server that is not there: final.
	other := "00000000-0000-4000-8000-000000000000"
	if _, err := n.State(ctx, other); !errors.As(err, &r) || !r.Refused() {
		t.Fatalf("an unknown server: %v", err)
	}
	if _, err := n.State(ctx, "../../servers"); err == nil {
		t.Fatal("a server id that is a path was used")
	}
}

func TestANovaNodeInTheFenceConfig(t *testing.T) {
	f := newFakeCloud(t)
	dir := t.TempDir()
	creds := filepath.Join(dir, "openstack")
	if err := os.MkdirAll(creds, 0o700); err != nil {
		t.Fatal(err)
	}
	_ = os.WriteFile(filepath.Join(creds, "application_credential_id"), []byte("cred-id\n"), 0o600)
	_ = os.WriteFile(filepath.Join(creds, "application_credential_secret"), []byte(novaSecret+"\n"), 0o600)
	write := func(body string) string {
		p := filepath.Join(dir, "fence.json")
		_ = os.WriteFile(p, []byte(body), 0o600)
		return p
	}
	cfg := `{"nodes": {"ctl-1": {"nova": {"authURL": "` + f.srv.URL + `/v3", "region": "RegionOne", "server": "` + novaServer + `", "credentials": "` + creds + `"}},
	                   "ctl-2": {"nova": {"authURL": "` + f.srv.URL + `/v3", "region": "RegionOne", "server": "11111111-2222-4333-8444-555555555555", "credentials": "` + creds + `"}}}}`
	router, machines, err := LoadConfig(write(cfg))
	if err != nil {
		t.Fatal(err)
	}
	if st, err := router.State(context.Background(), machines["ctl-1"]); err != nil || st != Running {
		t.Fatalf("through the router: %v %v", st, err)
	}
	for _, bad := range []string{
		// two nodes on one server: one machine's fence would be taken for the other's
		`{"nodes": {"a": {"nova": {"authURL": "` + f.srv.URL + `/v3", "server": "` + novaServer + `", "credentials": "` + creds + `"}},
		            "b": {"nova": {"authURL": "` + f.srv.URL + `/v3", "server": "` + novaServer + `", "credentials": "` + creds + `"}}}}`,
		`{"nodes": {"ctl-1": {"nova": {"authURL": "` + f.srv.URL + `/v3", "server": "not-a-uuid", "credentials": "` + creds + `"}}}}`,
		`{"nodes": {"ctl-1": {"nova": {"authURL": "` + f.srv.URL + `/v3", "server": "` + novaServer + `", "credentials": "` + dir + `/missing"}}}}`,
		`{"nodes": {"ctl-1": {"nova": {"authURL": "` + f.srv.URL + `/v3", "server": "` + novaServer + `", "credentials": "` + creds + `"},
		                      "ssh": {"addr": "h:22", "user": "u", "machine": "m", "credentials": "` + creds + `"}}}}`,
	} {
		if _, _, err := LoadConfig(write(bad)); err == nil {
			t.Errorf("accepted: %s", bad)
		} else if strings.Contains(err.Error(), novaSecret) {
			t.Errorf("the secret in an error: %v", err)
		}
	}
}
