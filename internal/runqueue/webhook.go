package runqueue

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path"
	"regexp"
	"strings"
)

// Hook is one webhook endpoint, /v1/hooks/{name}: a GitLab or GitHub project (or group) sends its
// events there, and Rules turn matching events into runs. The run's client is the hook's, its
// job and parameters come from the rule; nothing in the payload chooses a job.
type Hook struct {
	Provider string `json:"provider"` // gitlab | github
	// GitLab sends its secret token as is: only its SHA-256 is kept.
	TokenSHA256 string `json:"tokenSha256,omitempty"`
	// GitHub signs the body with HMAC-SHA256: the secret itself is needed, from this file.
	SecretFile string `json:"secretFile,omitempty"`
	Client     string `json:"client"`
	Rules      []Rule `json:"rules"`

	secret []byte
}

// Rule: an event of Event kind (push | tag | merge_request) whose ref matches Ref (a path.Match
// pattern on the branch or tag name) and whose project matches Project (a pattern on the
// project's full path; any if empty) starts Job with Parameters, where {{name}} is replaced by
// a field of the event (see fieldNames). As in GitLab's branch filters, * does not match "/":
// "release-*" matches release-1.2, "feature/*" is needed for feature/x.
type Rule struct {
	Event      string            `json:"event"`
	Ref        string            `json:"ref"`
	Project    string            `json:"project,omitempty"`
	Job        string            `json:"job"`
	Parameters map[string]string `json:"parameters,omitempty"`
}

var placeholder = regexp.MustCompile(`\{\{\s*([a-z_]+)\s*\}\}`)

// validate checks a hook at start and loads its secret.
func (h *Hook) validate(name string, c *Config) error {
	switch h.Provider {
	case "gitlab":
		if b, err := hex.DecodeString(h.TokenSHA256); err != nil || len(b) != sha256.Size {
			return fmt.Errorf("hook %s: a gitlab hook needs tokenSha256", name)
		}
	case "github":
		b, err := os.ReadFile(h.SecretFile)
		if err != nil || len(strings.TrimSpace(string(b))) < 16 {
			return fmt.Errorf("hook %s: a github hook needs a secretFile of at least 16 characters", name)
		}
		h.secret = []byte(strings.TrimSpace(string(b)))
	default:
		return fmt.Errorf("hook %s: provider must be gitlab or github", name)
	}
	if _, ok := c.Clients[h.Client]; !ok {
		return fmt.Errorf("hook %s: unknown client %s", name, h.Client)
	}
	if len(h.Rules) == 0 {
		return fmt.Errorf("hook %s has no rules", name)
	}
	for i, r := range h.Rules {
		if r.Event != "push" && r.Event != "tag" && r.Event != "merge_request" {
			return fmt.Errorf("hook %s rule %d: event must be push, tag or merge_request", name, i)
		}
		if _, err := path.Match(r.Ref, ""); err != nil || r.Ref == "" {
			return fmt.Errorf("hook %s rule %d: bad ref pattern %q", name, i, r.Ref)
		}
		if _, err := path.Match(r.Project, ""); err != nil {
			return fmt.Errorf("hook %s rule %d: bad project pattern %q", name, i, r.Project)
		}
		if msg := validJob(r.Job); msg != "" {
			return fmt.Errorf("hook %s rule %d: %s", name, i, msg)
		}
		if !c.MayTrigger(h.Client, r.Job) {
			return fmt.Errorf("hook %s rule %d: client %s may not trigger %s", name, i, h.Client, r.Job)
		}
		for k, v := range r.Parameters {
			for _, m := range placeholder.FindAllStringSubmatch(v, -1) {
				if !knownField(m[1]) {
					return fmt.Errorf("hook %s rule %d parameter %s: unknown field {{%s}}", name, i, k, m[1])
				}
			}
		}
	}
	return nil
}

// authentic checks the request came from the configured sender.
func (h *Hook) authentic(r *http.Request, body []byte) bool {
	switch h.Provider {
	case "gitlab":
		sum := sha256.Sum256([]byte(r.Header.Get("X-Gitlab-Token")))
		want, _ := hex.DecodeString(h.TokenSHA256)
		return subtle.ConstantTimeCompare(sum[:], want) == 1
	case "github":
		sig, ok := strings.CutPrefix(r.Header.Get("X-Hub-Signature-256"), "sha256=")
		if !ok {
			return false
		}
		got, err := hex.DecodeString(sig)
		if err != nil {
			return false
		}
		mac := hmac.New(sha256.New, h.secret)
		mac.Write(body)
		return hmac.Equal(got, mac.Sum(nil))
	}
	return false
}

// scmEvent is what rules and parameters are made from, provider-neutral.
type scmEvent struct {
	Kind     string // push | tag | merge_request | "" (ignored)
	Delivery string // stays the same when the sender retries: the idempotency key
	Fields   map[string]string
}

var fieldNames = []string{"ref", "ref_name", "sha", "before", "project", "user", "mr_iid", "mr_action", "source_branch", "target_branch"}

func knownField(f string) bool {
	for _, n := range fieldNames {
		if n == f {
			return true
		}
	}
	return false
}

const zeroSHA = "0000000000000000000000000000000000000000"

func parseGitLab(r *http.Request, body []byte) (scmEvent, error) {
	ev := scmEvent{Delivery: r.Header.Get("Idempotency-Key"), Fields: map[string]string{}}
	if ev.Delivery == "" {
		ev.Delivery = r.Header.Get("X-Gitlab-Event-UUID")
	}
	var p struct {
		ObjectKind  string `json:"object_kind"`
		Ref         string `json:"ref"`
		Before      string `json:"before"`
		After       string `json:"after"`
		CheckoutSHA string `json:"checkout_sha"`
		UserName    string `json:"user_username"`
		Project     struct {
			Path string `json:"path_with_namespace"`
		} `json:"project"`
		User struct {
			Username string `json:"username"`
		} `json:"user"`
		Attrs struct {
			IID          int    `json:"iid"`
			Action       string `json:"action"`
			SourceBranch string `json:"source_branch"`
			TargetBranch string `json:"target_branch"`
			LastCommit   struct {
				ID string `json:"id"`
			} `json:"last_commit"`
		} `json:"object_attributes"`
	}
	if err := json.Unmarshal(body, &p); err != nil {
		return ev, err
	}
	ev.Fields["project"] = p.Project.Path
	switch p.ObjectKind {
	case "push", "tag_push":
		if p.After == zeroSHA || p.CheckoutSHA == "" {
			return ev, nil // a deleted branch or tag: nothing to build
		}
		ev.Kind = "push"
		name, isBranch := strings.CutPrefix(p.Ref, "refs/heads/")
		if !isBranch {
			name, _ = strings.CutPrefix(p.Ref, "refs/tags/")
			ev.Kind = "tag"
		}
		ev.Fields["ref"], ev.Fields["ref_name"], ev.Fields["sha"], ev.Fields["before"], ev.Fields["user"] = p.Ref, name, p.CheckoutSHA, p.Before, p.UserName
	case "merge_request":
		ev.Kind = "merge_request"
		a := p.Attrs
		ev.Fields["ref"], ev.Fields["ref_name"], ev.Fields["sha"], ev.Fields["user"] = "refs/heads/"+a.SourceBranch, a.SourceBranch, a.LastCommit.ID, p.User.Username
		ev.Fields["mr_iid"], ev.Fields["mr_action"] = fmt.Sprint(a.IID), a.Action
		ev.Fields["source_branch"], ev.Fields["target_branch"] = a.SourceBranch, a.TargetBranch
	}
	return ev, nil
}

func parseGitHub(r *http.Request, body []byte) (scmEvent, error) {
	ev := scmEvent{Delivery: r.Header.Get("X-GitHub-Delivery"), Fields: map[string]string{}}
	var p struct {
		Ref     string `json:"ref"`
		Before  string `json:"before"`
		After   string `json:"after"`
		Deleted bool   `json:"deleted"`
		Action  string `json:"action"`
		Number  int    `json:"number"`
		Repo    struct {
			FullName string `json:"full_name"`
		} `json:"repository"`
		Sender struct {
			Login string `json:"login"`
		} `json:"sender"`
		PR struct {
			Head struct {
				Ref string `json:"ref"`
				SHA string `json:"sha"`
			} `json:"head"`
			Base struct {
				Ref string `json:"ref"`
			} `json:"base"`
		} `json:"pull_request"`
	}
	if err := json.Unmarshal(body, &p); err != nil {
		return ev, err
	}
	ev.Fields["project"], ev.Fields["user"] = p.Repo.FullName, p.Sender.Login
	switch r.Header.Get("X-GitHub-Event") {
	case "push":
		if p.Deleted || p.After == zeroSHA {
			return ev, nil
		}
		ev.Kind = "push"
		name, isBranch := strings.CutPrefix(p.Ref, "refs/heads/")
		if !isBranch {
			name, _ = strings.CutPrefix(p.Ref, "refs/tags/")
			ev.Kind = "tag"
		}
		ev.Fields["ref"], ev.Fields["ref_name"], ev.Fields["sha"], ev.Fields["before"] = p.Ref, name, p.After, p.Before
	case "pull_request":
		ev.Kind = "merge_request"
		ev.Fields["ref"], ev.Fields["ref_name"], ev.Fields["sha"] = "refs/heads/"+p.PR.Head.Ref, p.PR.Head.Ref, p.PR.Head.SHA
		ev.Fields["mr_iid"], ev.Fields["mr_action"] = fmt.Sprint(p.Number), p.Action
		ev.Fields["source_branch"], ev.Fields["target_branch"] = p.PR.Head.Ref, p.PR.Base.Ref
	}
	return ev, nil
}

// hookResult is the answer to the sender: which runs its event became.
type hookResult struct {
	Delivery string   `json:"delivery"`
	Runs     []string `json:"runs"`
	Ignored  string   `json:"ignored,omitempty"`
}

func (in *Intake) hook(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	h, ok := in.Config.Hooks[name]
	if !ok {
		problem(w, http.StatusNotFound, "no such hook")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 25<<20+1))
	if err != nil || len(body) > 25<<20 {
		problem(w, http.StatusRequestEntityTooLarge, "payload too large")
		return
	}
	if !h.authentic(r, body) {
		problem(w, http.StatusUnauthorized, "the request is not signed by this hook's secret")
		return
	}
	var ev scmEvent
	if h.Provider == "gitlab" {
		ev, err = parseGitLab(r, body)
	} else {
		ev, err = parseGitHub(r, body)
	}
	if err != nil {
		problem(w, http.StatusBadRequest, "the payload is not the provider's JSON")
		return
	}
	if ev.Delivery == "" {
		// Without the sender's delivery id a retried delivery would become a second run.
		problem(w, http.StatusBadRequest, "no delivery id (X-Gitlab-Event-UUID / Idempotency-Key / X-GitHub-Delivery)")
		return
	}
	res := hookResult{Delivery: ev.Delivery, Runs: []string{}}
	if ev.Kind == "" {
		res.Ignored = "not an event that builds (or a deleted ref)"
		writeJSON(w, http.StatusOK, res)
		return
	}
	for i, rule := range h.Rules {
		if !rule.matches(ev) {
			continue
		}
		cell, ok := in.Config.CellFor(rule.Job)
		if !ok {
			in.Log.Error("hook rule names a job no cell owns", "hook", name, "job", rule.Job)
			continue
		}
		params := map[string]string{}
		for k, v := range rule.Parameters {
			params[k] = placeholder.ReplaceAllStringFunc(v, func(m string) string {
				return ev.Fields[placeholder.FindStringSubmatch(m)[1]]
			})
		}
		run, created, err := in.Store.Accept(r.Context(), NewRun{Client: h.Client, Cell: cell, Job: rule.Job, Parameters: params,
			IdempotencyKey: fmt.Sprintf("hook:%s:%s:%d", name, ev.Delivery, i)})
		if errors.Is(err, ErrIdempotencyMismatch) {
			// The rule changed since this delivery was first accepted: the first run stands.
			in.Log.Warn("hook redelivery no longer matches its first run", "hook", name, "delivery", ev.Delivery, "rule", i)
			continue
		}
		if err != nil {
			// Not accepted, not acknowledged: the sender retries, with the same delivery id.
			in.Log.Error("accept hook run", "hook", name, "delivery", ev.Delivery, "error", err)
			problem(w, http.StatusServiceUnavailable, "not accepted; retry the delivery")
			return
		}
		if created {
			in.Metrics.accepted.WithLabelValues(cell, h.Client).Inc()
		}
		res.Runs = append(res.Runs, run.ID.String())
	}
	if len(res.Runs) == 0 {
		res.Ignored = "no rule matches"
	}
	writeJSON(w, http.StatusAccepted, res)
}

func (rule Rule) matches(ev scmEvent) bool {
	if rule.Event != ev.Kind {
		return false
	}
	if ok, _ := path.Match(rule.Ref, ev.Fields["ref_name"]); !ok {
		return false
	}
	if rule.Project != "" {
		if ok, _ := path.Match(rule.Project, ev.Fields["project"]); !ok {
			return false
		}
	}
	return true
}
