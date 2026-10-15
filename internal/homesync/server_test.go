package homesync

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

const (
	tokenR0 = "r0-token-0123456789abcdef0123456789abcdef"
	tokenR1 = "r1-token-0123456789abcdef0123456789abcdef"
)

func testServer(t *testing.T) *httptest.Server {
	t.Helper()
	store := testStore(t)
	file := filepath.Join(t.TempDir(), "replicas")
	os.WriteFile(file, []byte("# replicas\nr0:"+tokenR0+"\nr1:"+tokenR1+"\n"), 0o600) //nolint:errcheck
	replicas, err := LoadReplicas(file)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	srv := httptest.NewServer(NewServer(ctx, store, replicas, slog.New(slog.NewTextHandler(io.Discard, nil))).Handler())
	t.Cleanup(srv.Close)
	return srv
}

func call(t *testing.T, srv *httptest.Server, method, path, token string, headers map[string]string, body string) (int, http.Header, map[string]any, string) {
	t.Helper()
	req, _ := http.NewRequest(method, srv.URL+path, strings.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var decoded map[string]any
	json.Unmarshal(raw, &decoded) //nolint:errcheck
	return resp.StatusCode, resp.Header, decoded, string(raw)
}

func TestEveryCallNeedsAReplicasToken(t *testing.T) {
	srv := testServer(t)
	for _, token := range []string{"", "not-a-replica-token-0123456789abcdef"} {
		if code, _, _, _ := call(t, srv, "GET", "/v1/changes?after=0", token, nil, ""); code != http.StatusUnauthorized {
			t.Fatalf("token %q: %d", token, code)
		}
	}
}

// Which replica made a change is the server's to say: a request naming another replica is not believed.
func TestTheOriginOfAChangeComesFromTheCredential(t *testing.T) {
	srv := testServer(t)
	code, _, body, raw := call(t, srv, "PUT", "/v1/items?path=team/app", tokenR1,
		map[string]string{"If-Match": "0", "X-Replica": "r0", "X-Origin": "r0"}, "<flow-definition/>")
	if code != 200 || body["version"] != float64(1) {
		t.Fatalf("%d %s", code, raw)
	}
	_, header, _, _ := call(t, srv, "GET", "/v1/items?path=team/app", tokenR0, nil, "")
	if header.Get("X-Updated-By") != "r1" {
		t.Fatalf("recorded origin %q", header.Get("X-Updated-By"))
	}
	at, err := strconv.ParseInt(header.Get("X-Updated-At"), 10, 64)
	if err != nil || time.Since(time.UnixMilli(at)) > time.Minute || time.Since(time.UnixMilli(at)) < -time.Minute {
		t.Fatalf("X-Updated-At %q is not the time of the change", header.Get("X-Updated-At"))
	}
}

func TestTheAPIRefusesAStaleChangeAndALockHeldByAnother(t *testing.T) {
	srv := testServer(t)
	call(t, srv, "PUT", "/v1/items?path=quick", tokenR0, map[string]string{"If-Match": "0"}, "v1")
	call(t, srv, "PUT", "/v1/items?path=quick", tokenR0, map[string]string{"If-Match": "1"}, "v2")
	code, _, body, _ := call(t, srv, "PUT", "/v1/items?path=quick", tokenR1, map[string]string{"If-Match": "1"}, "stale")
	if code != http.StatusConflict || body["code"] != "VERSION_CONFLICT" || body["current"] != float64(2) {
		t.Fatalf("stale change: %d %v", code, body)
	}
	code, _, body, _ = call(t, srv, "POST", "/v1/locks?path=quick&ttl=10s", tokenR0, nil, "")
	if code != 200 || body["token"] == nil || body["head"] == nil {
		t.Fatalf("lock: %d %v", code, body)
	}
	if code, _, body, _ = call(t, srv, "POST", "/v1/locks?path=quick", tokenR1, nil, ""); code != http.StatusLocked || body["holder"] != "r0" {
		t.Fatalf("second lock: %d %v", code, body)
	}
	if code, _, _, _ = call(t, srv, "PUT", "/v1/items?path=quick", tokenR1, map[string]string{"If-Match": "2"}, "bg"); code != http.StatusLocked {
		t.Fatalf("an unlocked change past r0's lock: %d", code)
	}
	for _, bad := range []string{"/abs", "a/../b", "trailing/", ""} {
		if code, _, _, _ = call(t, srv, "PUT", "/v1/items?path="+bad, tokenR0, map[string]string{"If-Match": "0"}, "x"); code != http.StatusBadRequest {
			t.Fatalf("path %q: %d", bad, code)
		}
	}
	if code, _, _, _ = call(t, srv, "PUT", "/v1/items?path=nobody", tokenR0, nil, "x"); code != http.StatusBadRequest {
		t.Fatalf("a change without If-Match: %d", code)
	}
}

func TestALongPollWakesWhenAnotherReplicaCommits(t *testing.T) {
	srv := testServer(t)
	call(t, srv, "PUT", "/v1/items?path=a", tokenR0, map[string]string{"If-Match": "0"}, "x")
	_, _, first, _ := call(t, srv, "GET", "/v1/changes?after=0", tokenR1, nil, "")
	head := int64(first["head"].(float64))

	got := make(chan time.Duration, 1)
	start := time.Now()
	go func() {
		_, _, body, _ := call(t, srv, "GET", "/v1/changes?wait=20s&after="+itoa(head), tokenR1, nil, "")
		if list, _ := body["changes"].([]any); len(list) == 1 {
			got <- time.Since(start)
		} else {
			got <- -1
		}
	}()
	time.Sleep(300 * time.Millisecond)
	call(t, srv, "PUT", "/v1/items?path=b", tokenR0, map[string]string{"If-Match": "0"}, "y")
	select {
	case d := <-got:
		if d < 0 || d > 2*time.Second {
			t.Fatalf("the poll did not wake on the commit: %v", d)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the long poll never answered")
	}
	// Nothing new: it waits, then answers empty.
	start = time.Now()
	_, _, body, _ := call(t, srv, "GET", "/v1/changes?wait=1s&after="+itoa(head+1), tokenR1, nil, "")
	if list, _ := body["changes"].([]any); len(list) != 0 || time.Since(start) < 900*time.Millisecond {
		t.Fatalf("an empty poll: %v after %v", body, time.Since(start))
	}
}

func itoa(v int64) string { return strconv.FormatInt(v, 10) }

func TestTheJobLeaseAndReservationsOverHTTP(t *testing.T) {
	srv := testServer(t)
	code, _, body, raw := call(t, srv, "POST", "/v1/jobs/claim?job=team/app&ttl=10s", tokenR0, nil, "")
	if code != 200 || body["owner"] != "r0" || body["maxReserved"] != float64(0) {
		t.Fatalf("claim: %d %s", code, raw)
	}
	epoch := strconv.FormatInt(int64(body["epoch"].(float64)), 10)
	if code, _, body, _ = call(t, srv, "POST", "/v1/jobs/claim?job=team/app", tokenR1, nil, ""); code != http.StatusLocked || body["owner"] != "r0" {
		t.Fatalf("r1 claimed r0's job: %d %v", code, body)
	}
	if code, _, _, _ = call(t, srv, "POST", "/v1/jobs/reserve?job=team/app&number=1&epoch="+epoch, tokenR0, nil, ""); code != 200 {
		t.Fatalf("reserve: %d", code)
	}
	if code, _, body, _ = call(t, srv, "POST", "/v1/jobs/reserve?job=team/app&number=1&epoch="+epoch, tokenR0, nil, ""); code != 409 || body["code"] != "NUMBER_TAKEN" || body["max"] != float64(1) {
		t.Fatalf("a number twice: %d %v", code, body)
	}
	// r1 cannot reserve under r0's epoch: the epoch is not a credential, the token is.
	if code, _, body, _ = call(t, srv, "POST", "/v1/jobs/reserve?job=team/app&number=2&epoch="+epoch, tokenR1, nil, ""); code != 409 || body["code"] != "NOT_OWNER" {
		t.Fatalf("r1 reserved under r0's epoch: %d %v", code, body)
	}
	if code, _, body, _ = call(t, srv, "GET", "/v1/jobs/owner?job=team/app", tokenR1, nil, ""); code != 200 || body["owner"] != "r0" {
		t.Fatalf("owner: %d %v", code, body)
	}
}

func TestABuildMayBeLargerThanAConfigButIsStillBounded(t *testing.T) {
	srv := testServer(t)
	big := strings.Repeat("x", MaxItemBytes+1)
	if code, _, _, _ := call(t, srv, "PUT", "/v1/items?path=job", tokenR0, map[string]string{"If-Match": "0"}, big); code != http.StatusRequestEntityTooLarge {
		t.Fatalf("a config above 4 MiB: %d", code)
	}
	if code, _, _, raw := call(t, srv, "PUT", "/v1/items?path=@builds/job/1", tokenR0, map[string]string{"If-Match": "0"}, big); code != 200 {
		t.Fatalf("a build of 4 MiB: %d %s", code, raw)
	}
	if code, _, _, _ := call(t, srv, "PUT", "/v1/items?path=@builds/job/2", tokenR0, map[string]string{"If-Match": "0"}, strings.Repeat("y", MaxBuildBytes+1)); code != http.StatusRequestEntityTooLarge {
		t.Fatalf("a build above 32 MiB: %d", code)
	}
}

// A lock the store cannot take is the store's failure, a 503 that says nothing of the database;
// only a lifetime out of range is the caller's 400.
func TestALockTheStoreCannotTakeIs503NotTheCallersMistake(t *testing.T) {
	store := testStore(t)
	file := filepath.Join(t.TempDir(), "replicas")
	os.WriteFile(file, []byte("r0:"+tokenR0+"\n"), 0o600) //nolint:errcheck
	replicas, err := LoadReplicas(file)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	srv := httptest.NewServer(NewServer(ctx, store, replicas, slog.New(slog.NewTextHandler(io.Discard, nil))).Handler())
	t.Cleanup(srv.Close)
	if code, _, body, _ := call(t, srv, "POST", "/v1/locks?path=a&ttl=10m", tokenR0, nil, ""); code != http.StatusBadRequest {
		t.Fatalf("a 10 minute lock: %d %v", code, body)
	}
	cancel()           // the change listener holds a connection until then: Close would wait for it
	store.Pool.Close() // the database gone
	code, _, body, raw := call(t, srv, "POST", "/v1/locks?path=a&ttl=10s", tokenR0, nil, "")
	if code != http.StatusServiceUnavailable || body["code"] != "UNAVAILABLE" {
		t.Fatalf("a lock with the store gone: %d %v", code, body)
	}
	if strings.Contains(raw, "pool") || strings.Contains(raw, "closed") {
		t.Fatalf("the store's error reached the caller: %s", raw)
	}
}

func TestARunsNumberOverHTTP(t *testing.T) {
	srv := testServer(t)
	_, _, lease, _ := call(t, srv, "POST", "/v1/jobs/claim?job=app&ttl=1m", tokenR0, nil, "")
	epoch := strconv.FormatInt(int64(lease["epoch"].(float64)), 10)
	if code, _, body, _ := call(t, srv, "POST", "/v1/jobs/reserve?job=app&number=1&epoch="+epoch+"&run="+runA, tokenR0, nil, ""); code != 200 {
		t.Fatalf("reserve: %d %v", code, body)
	}
	code, _, body, _ := call(t, srv, "POST", "/v1/jobs/reserve?job=app&number=2&epoch="+epoch+"&run="+runA, tokenR0, nil, "")
	if code != http.StatusConflict || body["code"] != "RUN_TAKEN" || body["number"] != float64(1) {
		t.Fatalf("a second number: %d %v", code, body)
	}
	if code, _, _, _ := call(t, srv, "POST", "/v1/jobs/reserve?job=app&number=2&epoch="+epoch+"&run=NOT-A-UUID", tokenR0, nil, ""); code != http.StatusBadRequest {
		t.Fatalf("a run id that is not one: %d", code)
	}
	code, _, body, _ = call(t, srv, "GET", "/v1/runs?id="+runA, tokenR1, nil, "")
	if code != 200 || body["job"] != "app" || body["number"] != float64(1) || body["replica"] != "r0" || body["reservedAt"] == nil {
		t.Fatalf("lookup: %d %v", code, body)
	}
	if code, _, _, _ := call(t, srv, "DELETE", "/v1/runs?id="+runA+"&job=app&number=1", tokenR1, nil, ""); code != http.StatusNoContent {
		t.Fatalf("release: %d", code)
	}
	if code, _, _, _ := call(t, srv, "GET", "/v1/runs?id="+runA, tokenR1, nil, ""); code != http.StatusNotFound {
		t.Fatalf("lookup after the release: %d", code)
	}
	if code, _, _, _ := call(t, srv, "GET", "/v1/runs?id="+runA, "", nil, ""); code != http.StatusUnauthorized {
		t.Fatalf("lookup without a replica's token: %d", code)
	}
}

// A move over HTTP: refused with a code the plugin can tell apart, and done under the caller's locks.
func TestAMoveIsAskedDoneAndRefusedOverHTTP(t *testing.T) {
	srv := testServer(t)
	for _, p := range []string{"team", "team/app"} {
		if code, _, _, raw := call(t, srv, "PUT", "/v1/items?path="+p, tokenR0, map[string]string{"If-Match": "0"}, "<x/>"); code != 200 {
			t.Fatalf("%s: %d %s", p, code, raw)
		}
	}
	code, _, body, _ := call(t, srv, "POST", "/v1/locks?path=team&ttl=30s", tokenR1, nil, "")
	if code != 200 {
		t.Fatalf("lock: %d", code)
	}
	from := strconv.FormatInt(int64(body["token"].(float64)), 10)
	if code, _, body, _ := call(t, srv, "POST", "/v1/moves?from=team&to=crew", tokenR0, nil, ""); code != http.StatusLocked || body["holder"] != "r1" {
		t.Fatalf("a move of an item another replica is editing: %d %v", code, body)
	}
	if code, _, _, _ := call(t, srv, "POST", "/v1/moves?from=team&to=team/inner", tokenR1, map[string]string{"X-Lock-Token": from}, ""); code != 400 {
		t.Fatalf("into itself: %d", code)
	}
	if code, _, body, _ := call(t, srv, "POST", "/v1/moves?from=team&to=team/app", tokenR1, map[string]string{"X-Lock-Token": from}, ""); code != 400 {
		t.Fatalf("onto its own child: %d %v", code, body)
	}
	if code, _, body, _ := call(t, srv, "POST", "/v1/moves?from=nobody&to=crew", tokenR1, nil, ""); code != 404 || body["code"] != "NO_ITEM" {
		t.Fatalf("from nothing: %d %v", code, body)
	}
	if code, _, body, _ := call(t, srv, "POST", "/v1/moves?from=team/app&to=team", tokenR1, nil, ""); code != 409 || body["code"] != "EXISTS" {
		t.Fatalf("onto an item: %d %v", code, body)
	}
	if code, _, _, _ := call(t, srv, "POST", "/v1/moves?from=team&to=crew&dryRun=true", tokenR1, map[string]string{"X-Lock-Token": from}, ""); code != 200 {
		t.Fatalf("dry run: %d", code)
	}
	_, _, before, _ := call(t, srv, "GET", "/v1/changes?after=0", tokenR0, nil, "")
	code, _, body, raw := call(t, srv, "POST", "/v1/moves?from=team&to=crew", tokenR1, map[string]string{"X-Lock-Token": from}, "")
	if code != 200 || body["moved"] != float64(2) || body["version"] != float64(1) {
		t.Fatalf("move: %d %s", code, raw)
	}
	_, _, feed, _ := call(t, srv, "GET", "/v1/changes?after="+strconv.FormatInt(int64(before["head"].(float64)), 10), tokenR0, nil, "")
	first := feed["changes"].([]any)[0].(map[string]any)
	if first["path"] != "team" || first["movedTo"] != "crew" || first["deleted"] != true {
		t.Fatalf("the feed's first entry of the move: %v", first)
	}
	if code, _, body, _ := call(t, srv, "POST", "/v1/jobs/claim?job=team/app&ttl=15s", tokenR0, nil, ""); code != http.StatusGone || body["code"] != "GONE" {
		t.Fatalf("claiming the old name: %d %v", code, body)
	}
}
