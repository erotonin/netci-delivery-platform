package runqueue

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestTheJenkinsClientSpeaksThePluginsProtocol(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if u, p, _ := r.BasicAuth(); u != "netci" || p != "api-token" {
			w.WriteHeader(401)
			return
		}
		switch r.URL.Path {
		case "/netci/dispatch":
			_ = json.NewDecoder(r.Body).Decode(&got)
			_, _ = w.Write([]byte(`{"state":"queued","created":true,"session":"s1","queueId":7}`))
		case "/netci/run":
			if r.URL.Query().Get("job") == "missing" {
				w.WriteHeader(404)
				_, _ = w.Write([]byte(`{"error":"no such job, or no permission to see it","session":"s1"}`))
				return
			}
			w.WriteHeader(404)
			_, _ = w.Write([]byte(`{"runId":"x","state":"absent","session":"s2"}`))
		case "/html":
		}
	}))
	defer srv.Close()
	j := &Jenkins{BaseURL: srv.URL, User: "netci", Token: "api-token", Client: &http.Client{Timeout: time.Second}}
	r := &Run{ID: uuid.New(), Job: "payments/api", Parameters: map[string]string{"A": "1"}, AcceptedAt: time.Now()}
	st, err := j.Dispatch(context.Background(), r, "gitlab")
	if err != nil || st.State != "queued" || !st.Created || st.QueueID != 7 {
		t.Fatalf("%+v %v", st, err)
	}
	if got["runId"] != r.ID.String() || got["job"] != "payments/api" || got["requestedBy"] != "gitlab" || got["notBefore"] == nil {
		t.Fatalf("sent %v", got)
	}
	st, found, err := j.Lookup(context.Background(), r)
	if err != nil || found || st.Session != "s2" {
		t.Fatalf("absent: %+v %v %v", st, found, err)
	}
	_, _, err = j.Lookup(context.Background(), &Run{ID: uuid.New(), Job: "missing", AcceptedAt: time.Now()})
	var perm *PermanentError
	if !errors.As(err, &perm) || perm.Status != 404 {
		t.Fatalf("the plugin's refusal must be permanent: %v", err)
	}
}

func TestOnlyThePluginItselfCanRefuseARun(t *testing.T) {
	// Jenkins without the plugin, a 403 from Jenkins for a service account missing a permission,
	// and a 503 while starting are all HTML: configuration or timing, never a refusal.
	for _, code := range []int{403, 404, 503, 500} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/html")
			w.WriteHeader(code)
			_, _ = w.Write([]byte("<html>Jenkins</html>"))
		}))
		j := &Jenkins{BaseURL: srv.URL, User: "u", Token: "t", Client: &http.Client{Timeout: time.Second}}
		_, err := j.Dispatch(context.Background(), &Run{ID: uuid.New(), Job: "j", AcceptedAt: time.Now()}, "c")
		var perm *PermanentError
		if err == nil || errors.As(err, &perm) {
			t.Errorf("HTML %d treated as a refusal: %v", code, err)
		}
		srv.Close()
	}
}
