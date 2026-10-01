package runqueue

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/prometheus/client_golang/prometheus"
)

const gitlabPush = `{"object_kind":"push","ref":"refs/heads/main","before":"1111111111111111111111111111111111111111",
 "after":"2222222222222222222222222222222222222222","checkout_sha":"2222222222222222222222222222222222222222",
 "user_username":"alice","project":{"path_with_namespace":"payments/api"}}`

func hookAPI(t *testing.T) (*httptest.Server, *Store, string) {
	s := testStore(t)
	secretFile := filepath.Join(t.TempDir(), "github-secret")
	if err := os.WriteFile(secretFile, []byte("a-github-webhook-secret\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	c := testConfig()
	c.Hooks = map[string]*Hook{
		"payments": {Provider: "gitlab", TokenSHA256: hash("gitlab-hook-secret"), Client: "gitlab", Rules: []Rule{
			{Event: "push", Ref: "main", Job: "payments/api-main", Parameters: map[string]string{"GIT_SHA": "{{sha}}", "BRANCH": "{{ ref_name }}", "BY": "{{user}}"}},
			{Event: "push", Ref: "release-*", Job: "payments/api-release", Parameters: map[string]string{"GIT_SHA": "{{sha}}"}},
			{Event: "merge_request", Ref: "*", Job: "payments/api-mr", Parameters: map[string]string{"MR": "{{mr_iid}}", "TARGET": "{{target_branch}}"}},
		}},
		"gh": {Provider: "github", SecretFile: secretFile, Client: "release", Rules: []Rule{
			{Event: "push", Ref: "feature/*", Project: "acme/*", Job: "tools/lint", Parameters: map[string]string{"SHA": "{{sha}}"}},
		}},
	}
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	in := &Intake{Store: s, Config: c, Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Metrics: NewMetrics(prometheus.NewRegistry())}
	srv := httptest.NewServer(in.Handler())
	t.Cleanup(srv.Close)
	return srv, s, secretFile
}

func post(t *testing.T, url string, headers map[string]string, body string) (int, hookResult) {
	t.Helper()
	req, _ := http.NewRequest("POST", url, strings.NewReader(body))
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var res hookResult
	_ = json.NewDecoder(resp.Body).Decode(&res)
	return resp.StatusCode, res
}

func gitlab(delivery string) map[string]string {
	return map[string]string{"X-Gitlab-Token": "gitlab-hook-secret", "X-Gitlab-Event": "Push Hook", "Idempotency-Key": delivery,
		"X-Gitlab-Event-UUID": uuid.NewString()} // the UUID differs per attempt; the idempotency key does not
}

func TestAGitLabPushBecomesOneRunEvenWhenRedelivered(t *testing.T) {
	srv, s, _ := hookAPI(t)
	code, res := post(t, srv.URL+"/v1/hooks/payments", gitlab("delivery-1"), gitlabPush)
	if code != http.StatusAccepted || len(res.Runs) != 1 {
		t.Fatalf("%d %+v", code, res)
	}
	id := uuid.MustParse(res.Runs[0])
	run, err := s.Get(t.Context(), "gitlab", id)
	if err != nil {
		t.Fatal(err)
	}
	if run.Job != "payments/api-main" || run.Cell != "cell-b" || run.Parameters["GIT_SHA"] != "2222222222222222222222222222222222222222" ||
		run.Parameters["BRANCH"] != "main" || run.Parameters["BY"] != "alice" {
		t.Fatalf("%+v", run)
	}
	_, again := post(t, srv.URL+"/v1/hooks/payments", gitlab("delivery-1"), gitlabPush)
	if len(again.Runs) != 1 || again.Runs[0] != res.Runs[0] {
		t.Fatalf("a redelivery made another run: %+v", again)
	}
	var n int
	_ = s.Pool.QueryRow(t.Context(), `SELECT count(*) FROM runs`).Scan(&n)
	if n != 1 {
		t.Fatalf("%d runs", n)
	}
}

func TestWebhooksNotSignedByTheHooksSecretAreRefused(t *testing.T) {
	srv, s, _ := hookAPI(t)
	h := gitlab("d")
	h["X-Gitlab-Token"] = "guessed"
	if code, _ := post(t, srv.URL+"/v1/hooks/payments", h, gitlabPush); code != http.StatusUnauthorized {
		t.Fatal(code)
	}
	delete(h, "X-Gitlab-Token")
	if code, _ := post(t, srv.URL+"/v1/hooks/payments", h, gitlabPush); code != http.StatusUnauthorized {
		t.Fatal(code)
	}
	gh := map[string]string{"X-GitHub-Event": "push", "X-GitHub-Delivery": "g-1", "X-Hub-Signature-256": "sha256=" + strings.Repeat("00", 32)}
	if code, _ := post(t, srv.URL+"/v1/hooks/gh", gh, `{"ref":"refs/heads/x"}`); code != http.StatusUnauthorized {
		t.Fatal(code)
	}
	if code, _ := post(t, srv.URL+"/v1/hooks/nope", gitlab("d"), gitlabPush); code != http.StatusNotFound {
		t.Fatal(code)
	}
	var n int
	_ = s.Pool.QueryRow(t.Context(), `SELECT count(*) FROM runs`).Scan(&n)
	if n != 0 {
		t.Fatalf("%d runs from unauthenticated requests", n)
	}
}

func TestAGitHubPushIsVerifiedByItsHMAC(t *testing.T) {
	srv, s, _ := hookAPI(t)
	body := `{"ref":"refs/heads/feature/x","before":"a","after":"3333333333333333333333333333333333333333","repository":{"full_name":"acme/lint"},"sender":{"login":"bob"}}`
	mac := hmac.New(sha256.New, []byte("a-github-webhook-secret"))
	mac.Write([]byte(body))
	h := map[string]string{"X-GitHub-Event": "push", "X-GitHub-Delivery": "g-1", "X-Hub-Signature-256": "sha256=" + hex.EncodeToString(mac.Sum(nil))}
	code, res := post(t, srv.URL+"/v1/hooks/gh", h, body)
	if code != http.StatusAccepted || len(res.Runs) != 1 {
		t.Fatalf("%d %+v", code, res)
	}
	run, _ := s.Get(t.Context(), "release", uuid.MustParse(res.Runs[0]))
	if run.Job != "tools/lint" || run.Parameters["SHA"] != "3333333333333333333333333333333333333333" {
		t.Fatalf("%+v", run)
	}
	// The same body, one byte changed, under the same signature.
	if code, _ := post(t, srv.URL+"/v1/hooks/gh", h, strings.Replace(body, "feature/x", "feature/y", 1)); code != http.StatusUnauthorized {
		t.Fatalf("a tampered body was accepted: %d", code)
	}
}

func TestEventsThatShouldNotBuildDoNot(t *testing.T) {
	srv, _, _ := hookAPI(t)
	deleted := strings.Replace(gitlabPush, `"after":"2222222222222222222222222222222222222222","checkout_sha":"2222222222222222222222222222222222222222"`,
		`"after":"0000000000000000000000000000000000000000","checkout_sha":null`, 1)
	if _, res := post(t, srv.URL+"/v1/hooks/payments", gitlab("d1"), deleted); len(res.Runs) != 0 || res.Ignored == "" {
		t.Fatalf("a branch deletion built: %+v", res)
	}
	other := strings.Replace(gitlabPush, "refs/heads/main", "refs/heads/feature/x", 1)
	if _, res := post(t, srv.URL+"/v1/hooks/payments", gitlab("d2"), other); len(res.Runs) != 0 {
		t.Fatalf("a branch no rule names built: %+v", res)
	}
	release := strings.Replace(gitlabPush, "refs/heads/main", "refs/heads/release-1.2", 1)
	if _, res := post(t, srv.URL+"/v1/hooks/payments", gitlab("d3"), release); len(res.Runs) != 1 {
		t.Fatalf("%+v", res)
	}
	noDelivery := gitlab("")
	delete(noDelivery, "X-Gitlab-Event-UUID")
	if code, _ := post(t, srv.URL+"/v1/hooks/payments", noDelivery, gitlabPush); code != http.StatusBadRequest {
		t.Fatalf("a delivery without an id could be duplicated by a retry: %d", code)
	}
}

func TestAGitLabMergeRequestStartsTheMergeRequestJob(t *testing.T) {
	srv, s, _ := hookAPI(t)
	mr := `{"object_kind":"merge_request","user":{"username":"carol"},"project":{"path_with_namespace":"payments/api"},
	 "object_attributes":{"iid":42,"action":"open","source_branch":"fix-1","target_branch":"main","last_commit":{"id":"4444444444444444444444444444444444444444"}}}`
	_, res := post(t, srv.URL+"/v1/hooks/payments", gitlab("mr-1"), mr)
	if len(res.Runs) != 1 {
		t.Fatalf("%+v", res)
	}
	run, _ := s.Get(t.Context(), "gitlab", uuid.MustParse(res.Runs[0]))
	if run.Job != "payments/api-mr" || run.Parameters["MR"] != "42" || run.Parameters["TARGET"] != "main" {
		t.Fatalf("%+v", run)
	}
}

func TestHookConfigurationIsCheckedAtStart(t *testing.T) {
	ok := func() *Hook {
		return &Hook{Provider: "gitlab", TokenSHA256: hash("x"), Client: "gitlab", Rules: []Rule{{Event: "push", Ref: "main", Job: "payments/a"}}}
	}
	for name, mutate := range map[string]func(*Hook){
		"unknown provider":    func(h *Hook) { h.Provider = "bitbucket" },
		"no token":            func(h *Hook) { h.TokenSHA256 = "" },
		"unknown client":      func(h *Hook) { h.Client = "nobody" },
		"job outside client":  func(h *Hook) { h.Rules[0].Job = "tools/lint" },
		"unknown placeholder": func(h *Hook) { h.Rules[0].Parameters = map[string]string{"A": "{{secret}}"} },
		"bad event":           func(h *Hook) { h.Rules[0].Event = "issue" },
		"bad pattern":         func(h *Hook) { h.Rules[0].Ref = "[" },
		"github no secret":    func(h *Hook) { h.Provider, h.SecretFile = "github", "/nonexistent" },
	} {
		c := testConfig()
		h := ok()
		mutate(h)
		c.Hooks = map[string]*Hook{"h": h}
		if c.Validate() == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}
