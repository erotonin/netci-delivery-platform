package fabric

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// API serves the controllers' claims and the sandboxes' bindings.
//
//	POST   /v1/claims       (cell token)     {pool, agent, secret, controller} -> {id, pod, warm}
//	DELETE /v1/claims/{id}  (cell token)     the build is over: release the sandbox
//	GET    /v1/binding      (sandbox token)  long-poll: the binding once claimed; 204 while warm; 410 when over
//	GET    /v1/state        (sandbox token)  {state}: whether its claim still stands
type API struct {
	Store    *Store
	Client   kubernetes.Interface // TokenReview
	Pods     PodSettings
	Pools    map[string]Pool
	Cells    map[string]string // cell -> SHA-256 of its token
	Bindings *Bindings
	Recon    *Reconciler // creates a cold sandbox's pod at once
	Log      *slog.Logger
	Metrics  *Metrics
	LongPoll time.Duration
}

// Handler routes the API.
func (a *API) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/claims", a.claim)
	mux.HandleFunc("DELETE /v1/claims/{id}", a.release)
	mux.HandleFunc("GET /v1/binding", a.binding)
	mux.HandleFunc("GET /v1/state", a.state)
	return mux
}

var agentName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

type claimRequest struct {
	Pool       string `json:"pool"`
	Agent      string `json:"agent"`
	Secret     string `json:"secret"`
	Controller string `json:"controller"`
}

// cell authenticates a controller by its token; the token decides the cell.
func (a *API) cell(r *http.Request) (string, bool) {
	token, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	if !ok || token == "" {
		return "", false
	}
	sum := sha256.Sum256([]byte(token))
	found := ""
	for cell, h := range a.Cells {
		want, _ := hex.DecodeString(h)
		if subtle.ConstantTimeCompare(sum[:], want) == 1 {
			found = cell
		}
	}
	return found, found != ""
}

func (a *API) claim(w http.ResponseWriter, r *http.Request) {
	cell, ok := a.cell(r)
	if !ok {
		problem(w, http.StatusUnauthorized, "a cell token is required")
		return
	}
	raw, _ := io.ReadAll(io.LimitReader(r.Body, 16<<10))
	var fields map[string]json.RawMessage
	if json.Unmarshal(raw, &fields) != nil {
		problem(w, http.StatusBadRequest, "the body must be a JSON object")
		return
	}
	if _, named := fields["cell"]; named {
		problem(w, http.StatusUnprocessableEntity, "cell is decided by the token and may not be sent")
		return
	}
	var c claimRequest
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		problem(w, http.StatusBadRequest, "invalid body: "+err.Error())
		return
	}
	p, ok := a.Pools[c.Pool]
	if !ok {
		problem(w, http.StatusNotFound, "no such pool")
		return
	}
	u, err := url.Parse(c.Controller)
	switch {
	case !agentName.MatchString(c.Agent):
		problem(w, http.StatusBadRequest, "agent must be a Jenkins node name")
		return
	case len(c.Secret) < 32 || len(c.Secret) > 256:
		problem(w, http.StatusBadRequest, "secret must be the agent's inbound secret")
		return
	case err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil:
		problem(w, http.StatusBadRequest, "controller must be the controller's http(s) URL")
		return
	}
	claim := Claim{Pool: p.Name, Cell: cell, Agent: c.Agent, Controller: c.Controller}
	sb, err := a.Store.ClaimWarm(r.Context(), claim)
	warm := sb != nil
	if err == nil && sb == nil {
		live, lerr := a.Store.Live(r.Context())
		if lerr != nil {
			err = lerr
		} else if busy(live, p.Name) >= p.Max {
			problem(w, http.StatusTooManyRequests, "the pool is at its maximum; Jenkins will ask again")
			return
		} else {
			sb, err = a.Store.ClaimCold(r.Context(), claim)
		}
	}
	if err != nil {
		a.Log.Error("claim", "cell", cell, "pool", p.Name, "error", err)
		problem(w, http.StatusServiceUnavailable, "no sandbox could be claimed now")
		return
	}
	a.Bindings.Put(sb.ID, Binding{Controller: c.Controller, Agent: c.Agent, Secret: c.Secret})
	if !warm {
		a.Recon.createPod(r.Context(), sb)
	}
	a.Metrics.claims.WithLabelValues(p.Name, map[bool]string{true: "warm", false: "cold"}[warm]).Inc()
	a.Log.Info("claimed", "sandbox", sb.ID, "pod", sb.Pod, "pool", p.Name, "cell", cell, "agent", c.Agent, "warm", warm)
	writeJSON(w, http.StatusOK, map[string]any{"id": sb.ID.String(), "pod": sb.Pod, "warm": warm})
}

func busy(live []*Sandbox, pool string) int {
	n := 0
	for _, sb := range live {
		if sb.Pool == pool && (sb.State == Creating || sb.State == Warm || sb.State == Claimed || sb.State == Bound) {
			n++
		}
	}
	return n
}

func (a *API) release(w http.ResponseWriter, r *http.Request) {
	cell, ok := a.cell(r)
	if !ok {
		problem(w, http.StatusUnauthorized, "a cell token is required")
		return
	}
	id, err := uuid.Parse(r.PathValue("id"))
	if err != nil {
		problem(w, http.StatusNotFound, "no such claim")
		return
	}
	sb, err := a.Store.Get(r.Context(), id)
	if errors.Is(err, ErrNotFound) || (err == nil && sb.Cell != cell) {
		problem(w, http.StatusNotFound, "no such claim")
		return
	}
	if err != nil {
		problem(w, http.StatusServiceUnavailable, "try again")
		return
	}
	a.Bindings.Drop(id)
	if sb.State == Claimed || sb.State == Bound {
		if err := a.Store.Transition(r.Context(), id, sb.State, Released, "", "released by the controller"); err != nil && !errors.Is(err, ErrConflict) {
			problem(w, http.StatusServiceUnavailable, "try again")
			return
		}
		if sb.BoundAt != nil {
			a.Metrics.busySeconds.WithLabelValues(sb.Pool).Observe(time.Since(*sb.BoundAt).Seconds())
		}
	}
	w.WriteHeader(http.StatusNoContent)
}

// sandbox authenticates the calling pod by its projected token. The pod's identity comes from
// the TokenReview, never from the request.
func (a *API) sandbox(r *http.Request) (*Sandbox, int, string) {
	token, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	if !ok || token == "" {
		return nil, http.StatusUnauthorized, "a sandbox token is required"
	}
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	tr, err := a.Client.AuthenticationV1().TokenReviews().Create(ctx, &authenticationv1.TokenReview{
		Spec: authenticationv1.TokenReviewSpec{Token: token, Audiences: []string{a.Pods.Audience}},
	}, metav1.CreateOptions{})
	if err != nil {
		return nil, http.StatusServiceUnavailable, "cannot review the token now"
	}
	want := "system:serviceaccount:" + a.Pods.Namespace + ":" + a.Pods.ServiceAccount
	if !tr.Status.Authenticated || tr.Status.User.Username != want {
		return nil, http.StatusUnauthorized, "not a sandbox token"
	}
	pod, uid := first(tr.Status.User.Extra["authentication.kubernetes.io/pod-name"]), first(tr.Status.User.Extra["authentication.kubernetes.io/pod-uid"])
	if pod == "" || uid == "" {
		return nil, http.StatusUnauthorized, "the token is not bound to a pod"
	}
	sb, err := a.Store.ByPod(r.Context(), pod)
	if errors.Is(err, ErrNotFound) {
		return nil, http.StatusGone, "no sandbox for this pod"
	}
	if err != nil {
		return nil, http.StatusServiceUnavailable, "try again"
	}
	// A pod of the same name from an earlier sandbox cannot exist (names are fixed by ids), but
	// the uid also binds the token to this very pod.
	if sb.PodUID != "" && sb.PodUID != uid {
		return nil, http.StatusUnauthorized, "the token belongs to another pod"
	}
	if sb.PodUID == "" {
		_ = a.Store.SetPodUID(r.Context(), sb.ID, uid)
		sb.PodUID = uid
	}
	return sb, 0, ""
}

func first(v authenticationv1.ExtraValue) string {
	if len(v) == 0 {
		return ""
	}
	return v[0]
}

func (a *API) binding(w http.ResponseWriter, r *http.Request) {
	sb, code, msg := a.sandbox(r)
	if sb == nil {
		problem(w, code, msg)
		return
	}
	switch sb.State {
	case Released, Deleted, Failed:
		problem(w, http.StatusGone, "this sandbox is over")
		return
	case Creating, Warm:
		select {
		case <-a.Bindings.Wait(sb.ID):
			// Claimed meanwhile: read the row again for its state.
			if sb, _ = a.Store.Get(r.Context(), sb.ID); sb == nil {
				problem(w, http.StatusGone, "this sandbox is over")
				return
			}
		case <-time.After(a.LongPoll):
			w.WriteHeader(http.StatusNoContent)
			return
		case <-r.Context().Done():
			return
		}
	}
	bd, ok := a.Bindings.Get(sb.ID)
	if !ok && sb.State == Claimed {
		// The row says claimed a moment before the claim handler stores the binding.
		select {
		case <-a.Bindings.Wait(sb.ID):
		case <-time.After(5 * time.Second):
		case <-r.Context().Done():
			return
		}
		bd, ok = a.Bindings.Get(sb.ID)
	}
	if !ok {
		// Claimed, but this replica holds no binding (a failover): the claim cannot be served.
		problem(w, http.StatusGone, "the claim's binding is lost; the controller will provision again")
		return
	}
	if sb.State == Claimed {
		if err := a.Store.Transition(r.Context(), sb.ID, Claimed, Bound, "", ""); err == nil && sb.ClaimedAt != nil {
			a.Metrics.claimToBind.WithLabelValues(sb.Pool).Observe(time.Since(*sb.ClaimedAt).Seconds())
		}
	}
	writeJSON(w, http.StatusOK, bd)
}

func (a *API) state(w http.ResponseWriter, r *http.Request) {
	sb, code, msg := a.sandbox(r)
	if sb == nil {
		problem(w, code, msg)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"state": string(sb.State)})
}

func problem(w http.ResponseWriter, status int, detail string) {
	w.Header().Set("Content-Type", "application/problem+json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"status": status, "detail": detail})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
