package runqueue

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

func hash(token string) string {
	s := sha256.Sum256([]byte(token))
	return hex.EncodeToString(s[:])
}

func testConfig() *Config {
	return &Config{
		Cells:  map[string]CellConfig{"cell-b": {URL: "http://jenkins.cell-b.svc:8080", Credentials: "/x", Budget: 5}, "cell-c": {URL: "http://c", Credentials: "/x", Budget: 5}},
		Routes: []Route{{Prefix: "", Cell: "cell-c"}, {Prefix: "payments/", Cell: "cell-b"}},
		Clients: map[string]ClientConfig{
			"gitlab":  {TokenSHA256: hash("gitlab-token"), Jobs: []string{"payments/"}},
			"release": {TokenSHA256: hash("release-token"), Jobs: []string{""}},
			"cell-b":  {TokenSHA256: hash("cell-b-token"), Once: true},
		},
	}
}

type api struct {
	t   *testing.T
	srv *httptest.Server
}

func newAPI(t *testing.T) (*api, *Store) {
	s := testStore(t)
	in := &Intake{Store: s, Config: testConfig(), Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Metrics: NewMetrics(prometheus.NewRegistry())}
	srv := httptest.NewServer(in.Handler())
	t.Cleanup(srv.Close)
	return &api{t: t, srv: srv}, s
}

func (a *api) do(method, path, token, body string) (int, map[string]any) {
	a.t.Helper()
	req, _ := http.NewRequest(method, a.srv.URL+path, strings.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestIntakeAcceptsARunAndRoutesItToTheCellThatOwnsTheJob(t *testing.T) {
	a, _ := newAPI(t)
	code, run := a.do("POST", "/v1/runs", "gitlab-token", `{"job":"payments/api","parameters":{"SHA":"abc"},"idempotencyKey":"d-1"}`)
	if code != http.StatusAccepted || run["state"] != "accepted" || run["cell"] != "cell-b" {
		t.Fatalf("%d %v", code, run)
	}
	code, again := a.do("POST", "/v1/runs", "gitlab-token", `{"job":"payments/api","parameters":{"SHA":"abc"},"idempotencyKey":"d-1"}`)
	if code != http.StatusOK || again["id"] != run["id"] {
		t.Fatalf("a redelivery made a new run: %d %v", code, again)
	}
	code, _ = a.do("POST", "/v1/runs", "gitlab-token", `{"job":"payments/api","parameters":{"SHA":"zzz"},"idempotencyKey":"d-1"}`)
	if code != http.StatusConflict {
		t.Fatalf("a reused key for another run: %d", code)
	}
	code, other := a.do("POST", "/v1/runs", "release-token", `{"job":"tools/lint"}`)
	if code != http.StatusAccepted || other["cell"] != "cell-c" {
		t.Fatalf("%d %v", code, other)
	}
	code, got := a.do("GET", "/v1/runs/"+run["id"].(string), "gitlab-token", "")
	if code != http.StatusOK || len(got["history"].([]any)) != 1 {
		t.Fatalf("%d %v", code, got)
	}
}

func TestIntakeRefusesWhatTheServerDecides(t *testing.T) {
	a, s := newAPI(t)
	for _, f := range []string{`"client":"admin"`, `"cell":"cell-c"`, `"state":"finished"`, `"id":"00000000-0000-0000-0000-000000000000"`} {
		code, body := a.do("POST", "/v1/runs", "gitlab-token", `{"job":"payments/api",`+f+`}`)
		if code != http.StatusUnprocessableEntity {
			t.Fatalf("%s: %d %v", f, code, body)
		}
	}
	var n int
	_ = s.Pool.QueryRow(t.Context(), `SELECT count(*) FROM runs`).Scan(&n)
	if n != 0 {
		t.Fatalf("%d runs stored from refused requests", n)
	}
}

func TestIntakeAuthenticatesAndAuthorises(t *testing.T) {
	a, _ := newAPI(t)
	if code, _ := a.do("POST", "/v1/runs", "", `{"job":"payments/api"}`); code != http.StatusUnauthorized {
		t.Fatal(code)
	}
	if code, _ := a.do("POST", "/v1/runs", "wrong", `{"job":"payments/api"}`); code != http.StatusUnauthorized {
		t.Fatal(code)
	}
	if code, _ := a.do("POST", "/v1/runs", "gitlab-token", `{"job":"tools/lint"}`); code != http.StatusForbidden {
		t.Fatalf("a client triggered a job outside its prefixes: %d", code)
	}
	_, run := a.do("POST", "/v1/runs", "release-token", `{"job":"payments/api"}`)
	if code, _ := a.do("GET", "/v1/runs/"+run["id"].(string), "gitlab-token", ""); code != http.StatusNotFound {
		t.Fatalf("a client read another client's run: %d", code)
	}
}

func TestIntakeRejectsMalformedRequests(t *testing.T) {
	a, _ := newAPI(t)
	for _, body := range []string{
		`not json`, `[]`, `{"job":""}`, `{"job":"/abs"}`, `{"job":"payments/../admin"}`, `{"job":"payments/api","extra":1}`,
		`{"job":"payments/api","parameters":{"A":1}}`, `{"job":"payments/api","parameters":{"":"x"}}`,
	} {
		if code, _ := a.do("POST", "/v1/runs", "gitlab-token", body); code != http.StatusBadRequest {
			t.Errorf("%s: %d", body, code)
		}
	}
	big := `{"job":"payments/api","parameters":{"A":"` + strings.Repeat("x", maxIntakeBody) + `"}}`
	if code, _ := a.do("POST", "/v1/runs", "gitlab-token", big); code != http.StatusRequestEntityTooLarge {
		t.Errorf("oversized body: %d", code)
	}
}

func TestConfigIsCheckedAtStart(t *testing.T) {
	c := testConfig()
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	if cell, _ := c.CellFor("payments/api"); cell != "cell-b" {
		t.Fatal("longest prefix must win", cell)
	}
	bad := []func(*Config){
		func(c *Config) { c.Clients = nil },
		func(c *Config) { c.Cells = nil },
		func(c *Config) { c.Routes = append(c.Routes, Route{Prefix: "x/", Cell: "nowhere"}) },
		func(c *Config) { c.Routes = append(c.Routes, Route{Prefix: "payments/", Cell: "cell-c"}) },
		func(c *Config) {
			c.Clients["dup"] = ClientConfig{TokenSHA256: hash("gitlab-token"), Jobs: []string{"x"}}
		},
		func(c *Config) { c.Clients["short"] = ClientConfig{TokenSHA256: "abcd", Jobs: []string{"x"}} },
		func(c *Config) { c.Clients["idle"] = ClientConfig{TokenSHA256: hash("idle-token")} },
		func(c *Config) { c.Cells["cell-b"] = CellConfig{URL: "ftp://x", Credentials: "/x", Budget: 1} },
		func(c *Config) { c.Cells["cell-b"] = CellConfig{URL: "http://x", Credentials: "/x", Budget: 0} },
	}
	for i, f := range bad {
		c := testConfig()
		f(c)
		if c.Validate() == nil {
			t.Errorf("case %d accepted", i)
		}
	}
}
