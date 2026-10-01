package fabric

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
	clocktesting "k8s.io/utils/clock/testing"
)

func testStore(t *testing.T) *Store {
	t.Helper()
	url := os.Getenv("NETCI_QUEUE_TEST_DATABASE_URL")
	if url == "" {
		t.Skip("NETCI_QUEUE_TEST_DATABASE_URL is not set: the fabric's PostgreSQL tests did not run")
	}
	pool, err := pgxpool.New(context.Background(), url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := Migrate(context.Background(), pool); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(context.Background(), `TRUNCATE sandbox_events, sandboxes`); err != nil {
		t.Fatal(err)
	}
	return &Store{Pool: pool}
}

const ns = "netci-agents"

type world struct {
	t     *testing.T
	store *Store
	kube  *fake.Clientset
	clk   *clocktesting.FakeClock
	recon *Reconciler
	api   *API
	srv   *httptest.Server
}

func hash(s string) string { h := sha256.Sum256([]byte(s)); return hex.EncodeToString(h[:]) }

func newWorld(t *testing.T, warm, max int) *world {
	s := testStore(t)
	kube := fake.NewClientset()
	// A sandbox token here is "pod:<name>:<uid>"; the review says which pod it belongs to, as the
	// API server does for a projected token.
	kube.PrependReactor("create", "tokenreviews", func(a k8stesting.Action) (bool, runtime.Object, error) {
		tr := a.(k8stesting.CreateAction).GetObject().(*authenticationv1.TokenReview)
		parts := strings.Split(tr.Spec.Token, ":")
		if len(parts) != 3 || parts[0] != "pod" || len(tr.Spec.Audiences) != 1 || tr.Spec.Audiences[0] != "netci-fabric" {
			return true, &authenticationv1.TokenReview{}, nil
		}
		tr.Status = authenticationv1.TokenReviewStatus{Authenticated: true, User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:" + ns + ":netci-sandbox",
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {parts[1]}, "authentication.kubernetes.io/pod-uid": {parts[2]}},
		}}
		return true, tr, nil
	})
	clk := clocktesting.NewFakeClock(time.Now())
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	m := NewMetrics(prometheus.NewRegistry())
	pools := map[string]Pool{"standard": {Name: "standard", Labels: []string{"linux"}, Image: "jenkins/inbound-agent:x",
		Warm: warm, Max: max, CPU: "1", Memory: "1Gi", Disk: "4Gi", UserNamespace: true}}
	ps := PodSettings{Namespace: ns, ServiceAccount: "netci-sandbox", BootstrapImage: "netci/netci:x", FabricURL: "http://fabric", Audience: "netci-fabric"}
	b := NewBindings()
	recon := &Reconciler{Store: s, Client: kube, Pods: ps, Pools: pools, Bindings: b, Clock: clk, Log: log, Metrics: m,
		BindTimeout: 2 * time.Minute, StartTimeout: 5 * time.Minute}
	api := &API{Store: s, Client: kube, Pods: ps, Pools: pools, Cells: map[string]string{"cell-b": hash("cell-b-token"), "cell-c": hash("cell-c-token")},
		Bindings: b, Recon: recon, Log: log, Metrics: m, LongPoll: 300 * time.Millisecond}
	srv := httptest.NewServer(api.Handler())
	t.Cleanup(srv.Close)
	return &world{t: t, store: s, kube: kube, clk: clk, recon: recon, api: api, srv: srv}
}

func (w *world) tick() {
	w.t.Helper()
	if err := w.recon.Tick(context.Background()); err != nil {
		w.t.Fatal(err)
	}
}

func (w *world) pods() []corev1.Pod {
	l, _ := w.kube.CoreV1().Pods(ns).List(context.Background(), metav1.ListOptions{})
	return l.Items
}

// start makes every pod Running and Ready, with a uid, as the kubelet would.
func (w *world) start() {
	for _, p := range w.pods() {
		if p.Status.Phase == corev1.PodRunning {
			continue
		}
		p.UID = types.UID("uid-" + p.Name)
		p.Status.Phase = corev1.PodRunning
		p.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}
		if _, err := w.kube.CoreV1().Pods(ns).Update(context.Background(), &p, metav1.UpdateOptions{}); err != nil {
			w.t.Fatal(err)
		}
	}
}

func (w *world) states() map[State]int {
	live, _ := w.store.Live(context.Background())
	out := map[State]int{}
	for _, sb := range live {
		out[sb.State]++
	}
	return out
}

func (w *world) req(method, path, token, body string) (int, map[string]any) {
	w.t.Helper()
	r, _ := http.NewRequest(method, w.srv.URL+path, strings.NewReader(body))
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(r)
	if err != nil {
		w.t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

const secret = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

func claimBody(agent string) string {
	return `{"pool":"standard","agent":"` + agent + `","secret":"` + secret + `","controller":"http://jenkins.cell-b.svc:8080/"}`
}

func TestThePoolIsKeptWarmAndSandboxPodsCarryNoAPICredential(t *testing.T) {
	w := newWorld(t, 2, 5)
	w.tick()
	if got := w.states(); got[Creating] != 2 || len(w.pods()) != 2 {
		t.Fatalf("%v, %d pods", got, len(w.pods()))
	}
	w.start()
	w.tick()
	if got := w.states(); got[Warm] != 2 {
		t.Fatalf("%v", got)
	}
	w.tick()
	if len(w.pods()) != 2 {
		t.Fatal("over-filled the pool")
	}
	p := w.pods()[0]
	if p.Spec.AutomountServiceAccountToken == nil || *p.Spec.AutomountServiceAccountToken {
		t.Fatal("a sandbox gets the API token automatically")
	}
	if p.Spec.HostUsers == nil || *p.Spec.HostUsers {
		t.Fatal("no user namespace")
	}
	c := p.Spec.Containers[0]
	if c.SecurityContext == nil || *c.SecurityContext.AllowPrivilegeEscalation || len(c.SecurityContext.Capabilities.Drop) != 1 {
		t.Fatal("the build container is not restricted")
	}
	if c.Resources.Limits.Memory().String() != "1Gi" || c.Resources.Limits.StorageEphemeral().String() != "4Gi" {
		t.Fatal("no limits")
	}
	var aud string
	for _, v := range p.Spec.Volumes {
		if v.Projected != nil {
			aud = v.Projected.Sources[0].ServiceAccountToken.Audience
		}
	}
	if aud != "netci-fabric" {
		t.Fatal("the identity token is not scoped to the fabric")
	}
}

func TestASandboxStillBeingMadeWarmIsAnsweredAtOnce(t *testing.T) {
	w := newWorld(t, 1, 3)
	w.api.LongPoll = 10 * time.Second
	w.tick()
	w.start()
	pod := w.pods()[0]
	began := time.Now()
	code, _ := w.req("GET", "/v1/binding", "pod:"+pod.Name+":uid-"+pod.Name, "")
	if code != http.StatusNoContent || time.Since(began) > 2*time.Second {
		t.Fatalf("a creating sandbox waited %s for %d: it stays out of the pool that long", time.Since(began), code)
	}
}

func TestAClaimBindsAWarmSandboxAndOnlyThatPodGetsTheSecret(t *testing.T) {
	w := newWorld(t, 1, 3)
	w.tick()
	w.start()
	w.tick()
	pod := w.pods()[0]
	token := "pod:" + pod.Name + ":uid-" + pod.Name

	// The warm sandbox waits; a claim arrives while it does.
	type answer struct {
		code int
		body map[string]any
	}
	got := make(chan answer, 1)
	go func() {
		c, b := w.req("GET", "/v1/binding", token, "")
		got <- answer{c, b}
	}()
	time.Sleep(50 * time.Millisecond)
	code, claim := w.req("POST", "/v1/claims", "cell-b-token", claimBody("netci-standard-abc"))
	if code != 200 || claim["warm"] != true || claim["pod"] != pod.Name {
		t.Fatalf("%d %v", code, claim)
	}
	a := <-got
	if a.code != 200 || a.body["secret"] != secret || a.body["agent"] != "netci-standard-abc" {
		t.Fatalf("%d %v", a.code, a.body)
	}
	if w.states()[Bound] != 1 {
		t.Fatalf("%v", w.states())
	}
	// Another pod cannot read it, nor can a token for this pod's name from another pod.
	if c, _ := w.req("GET", "/v1/binding", "pod:"+pod.Name+":some-other-uid", ""); c != http.StatusUnauthorized {
		t.Fatalf("a different pod with the same name read the binding: %d", c)
	}
	if c, _ := w.req("GET", "/v1/binding", "not-a-projected-token", ""); c != http.StatusUnauthorized {
		t.Fatalf("%d", c)
	}
	// The secret is nowhere in the database.
	var n int
	_ = w.store.Pool.QueryRow(context.Background(), `SELECT count(*) FROM sandboxes s JOIN sandbox_events e ON e.sandbox_id = s.id
		WHERE row_to_json(s)::text LIKE '%'||$1||'%' OR e.detail::text LIKE '%'||$1||'%'`, secret).Scan(&n)
	if n != 0 {
		t.Fatal("the agent's secret was written to the database")
	}
}

func TestTheTokenDecidesTheCell(t *testing.T) {
	w := newWorld(t, 1, 3)
	w.tick()
	w.start()
	w.tick()
	if c, _ := w.req("POST", "/v1/claims", "", claimBody("a")); c != 401 {
		t.Fatal(c)
	}
	if c, _ := w.req("POST", "/v1/claims", "cell-b-token", strings.Replace(claimBody("a"), `{"pool"`, `{"cell":"cell-c","pool"`, 1)); c != 422 {
		t.Fatalf("a claim naming its cell: %d", c)
	}
	_, claim := w.req("POST", "/v1/claims", "cell-b-token", claimBody("a"))
	if c, _ := w.req("DELETE", "/v1/claims/"+claim["id"].(string), "cell-c-token", ""); c != 404 {
		t.Fatalf("another cell released this claim: %d", c)
	}
	if c, _ := w.req("DELETE", "/v1/claims/"+claim["id"].(string), "cell-b-token", ""); c != 204 {
		t.Fatal(c)
	}
	for _, bad := range []string{
		`{"pool":"nope","agent":"a","secret":"` + secret + `","controller":"http://x"}`,
		`{"pool":"standard","agent":"../x","secret":"` + secret + `","controller":"http://x"}`,
		`{"pool":"standard","agent":"a","secret":"short","controller":"http://x"}`,
		`{"pool":"standard","agent":"a","secret":"` + secret + `","controller":"file:///etc/passwd"}`,
	} {
		if c, _ := w.req("POST", "/v1/claims", "cell-b-token", bad); c != 400 && c != 404 {
			t.Errorf("%s: %d", bad, c)
		}
	}
}

func TestAClaimWithNothingWarmCreatesASandboxAtOnceUpToTheMaximum(t *testing.T) {
	w := newWorld(t, 0, 2)
	for i, want := range []int{200, 200, 429} {
		c, body := w.req("POST", "/v1/claims", "cell-b-token", claimBody("agent-"+string(rune('a'+i))))
		if c != want {
			t.Fatalf("claim %d: %d %v", i, c, body)
		}
		if c == 200 && body["warm"] != false {
			t.Fatal(body)
		}
	}
	if len(w.pods()) != 2 {
		t.Fatalf("%d pods created for 2 cold claims", len(w.pods()))
	}
}

func TestReleasedAbandonedAndOrphanedSandboxesAreCleanedUp(t *testing.T) {
	w := newWorld(t, 2, 4)
	w.tick()
	w.start()
	w.tick()
	_, used := w.req("POST", "/v1/claims", "cell-b-token", claimBody("used"))
	_, abandoned := w.req("POST", "/v1/claims", "cell-b-token", claimBody("abandoned"))
	usedPod := used["pod"].(string)
	w.req("GET", "/v1/binding", "pod:"+usedPod+":uid-"+usedPod, "")
	w.req("DELETE", "/v1/claims/"+used["id"].(string), "cell-b-token", "")
	// A pod nobody accounts for.
	_, _ = w.kube.CoreV1().Pods(ns).Create(context.Background(), &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Name: "sbx-stray", Namespace: ns, Labels: map[string]string{labelSandbox: uuid.NewString()}}}, metav1.CreateOptions{})
	w.clk.Step(3 * time.Minute) // the abandoned claim was never bound
	w.tick()
	names := map[string]bool{}
	for _, p := range w.pods() {
		names[p.Name] = true
	}
	if names[usedPod] || names[abandoned["pod"].(string)] || names["sbx-stray"] {
		t.Fatalf("pods left: %v", names)
	}
	w.tick() // pods gone: rows deleted; the pool refilled
	w.start()
	w.tick()
	if got := w.states(); got[Warm] != 2 || got[Released] != 0 || got[Claimed] != 0 {
		t.Fatalf("%v", got)
	}
}

func TestConcurrentClaimsNeverShareASandbox(t *testing.T) {
	w := newWorld(t, 5, 20)
	w.tick()
	w.start()
	w.tick()
	var mu sync.Mutex
	pods := map[string]int{}
	var wg sync.WaitGroup
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			c, body := w.req("POST", "/v1/claims", "cell-b-token", claimBody("agent-"+uuid.NewString()[:8]))
			if c != 200 {
				t.Error(c, body)
				return
			}
			mu.Lock()
			pods[body["pod"].(string)]++
			mu.Unlock()
		}(i)
	}
	wg.Wait()
	for p, n := range pods {
		if n != 1 {
			t.Fatalf("pod %s given to %d claims", p, n)
		}
	}
	if len(pods) != 12 {
		t.Fatalf("%d distinct sandboxes for 12 claims", len(pods))
	}
}
