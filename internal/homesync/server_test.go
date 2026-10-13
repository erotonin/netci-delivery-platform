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
