// Package sandbox is the entrypoint of a build sandbox (ADR-064): it waits, warm, for the fabric
// to bind it to a controller, then runs the Jenkins agent and keeps it attached to whichever
// controller serves that address, until the fabric says its build is over.
package sandbox

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"
)

// Binding mirrors the fabric's.
type Binding struct {
	Controller string `json:"controller"`
	Agent      string `json:"agent"`
	Secret     string `json:"secret"`
}

// Bootstrap is one sandbox's life.
type Bootstrap struct {
	FabricURL string
	TokenFile string // projected ServiceAccount token, re-read on every call (it rotates)
	Java      string
	AgentJar  string
	WorkDir   string
	SecretDir string // where the agent's secret is written for agent.jar's -secret @file
	ReadyFile string // created once the fabric has answered: the pod is warm
	Client    *http.Client
	Log       *slog.Logger
	// SessionPoll: how often the controller's X-Jenkins-Session is checked. A controller lost to
	// power never closes the agent's connection; a new session means a new controller.
	SessionPoll time.Duration
	Retry       time.Duration
	// SecretTTL: the secret file is removed this long after the agent started (it reads it once).
	SecretTTL time.Duration
	// JarCache, when set, is a read-only remoting jar cache (the pool's jarCache image) copied into
	// WorkDir's before the sandbox is warm: the agent then loads the controller's jars from disk
	// instead of fetching ~19 MB of them over its channel while it connects (ADR-072).
	JarCache string
}

// ErrOver: the fabric says this sandbox's build is over.
var errOver = errors.New("sandbox released")

// Run waits for the binding and keeps the agent running until the sandbox is released or ctx
// ends. It returns nil when released.
func (b *Bootstrap) Run(ctx context.Context) error {
	b.seedJarCache()
	bd, err := b.waitBinding(ctx)
	if errors.Is(err, errOver) {
		return nil
	}
	if err != nil {
		return err
	}
	b.Log.Info("bound", "controller", bd.Controller, "agent", bd.Agent)
	for {
		err := b.runAgent(ctx, bd)
		if ctx.Err() != nil {
			return nil
		}
		state, serr := b.state(ctx)
		if serr == nil && state != "claimed" && state != "bound" {
			b.Log.Info("the fabric released this sandbox: stopping", "state", state)
			return nil
		}
		if errors.Is(serr, errOver) {
			return nil
		}
		b.Log.Warn("agent stopped; starting it again", "error", err)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(b.Retry):
		}
	}
}

func (b *Bootstrap) waitBinding(ctx context.Context) (Binding, error) {
	for {
		var bd Binding
		code, err := b.call(ctx, "/v1/binding", &bd)
		switch {
		case err == nil && code == http.StatusOK:
			b.ready()
			return bd, nil
		case err == nil && code == http.StatusNoContent:
			b.ready()
			// Still warm (after a long poll) or still being made warm (answered at once).
			select {
			case <-ctx.Done():
				return Binding{}, ctx.Err()
			case <-time.After(250 * time.Millisecond):
			}
			continue
		case err == nil && code == http.StatusGone:
			return Binding{}, errOver
		}
		b.Log.Warn("waiting for the fabric", "status", code, "error", err)
		select {
		case <-ctx.Done():
			return Binding{}, ctx.Err()
		case <-time.After(b.Retry):
		}
	}
}

// A remoting jar cache entry: <first byte of the checksum>/<the rest>.jar, in upper-case hex.
var (
	cacheDir  = regexp.MustCompile(`^[0-9A-F]{2}$`)
	cacheFile = regexp.MustCompile(`^[0-9A-F]{30}\.jar$`)
)

// seedJarCache copies JarCache into the agent's own jar cache. Only entries named as remoting
// names them, regular files, and nothing already there; a failure is logged and leaves the agent
// to fetch what is missing, as it would without a cache.
func (b *Bootstrap) seedJarCache() {
	if b.JarCache == "" {
		return
	}
	start := time.Now()
	dst := filepath.Join(b.WorkDir, "remoting", "jarCache")
	dirs, err := os.ReadDir(b.JarCache)
	if err != nil {
		b.Log.Warn("no jar cache to seed from", "dir", b.JarCache, "error", err)
		return
	}
	files, bytes := 0, int64(0)
	for _, d := range dirs {
		if !d.IsDir() || !cacheDir.MatchString(d.Name()) {
			continue
		}
		entries, err := os.ReadDir(filepath.Join(b.JarCache, d.Name()))
		if err != nil {
			continue
		}
		for _, e := range entries {
			if !e.Type().IsRegular() || !cacheFile.MatchString(e.Name()) {
				continue
			}
			n, err := copyNew(filepath.Join(b.JarCache, d.Name(), e.Name()), filepath.Join(dst, d.Name(), e.Name()))
			if err != nil {
				b.Log.Warn("jar cache entry not seeded", "entry", d.Name()+"/"+e.Name(), "error", err)
				continue
			}
			files, bytes = files+1, bytes+n
		}
	}
	b.Log.Info("jar cache seeded", "jars", files, "bytes", bytes, "seconds", time.Since(start).Seconds())
}

// copyNew copies src to dst, which must not exist yet.
func copyNew(src, dst string) (int64, error) {
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return 0, err
	}
	in, err := os.Open(src)
	if err != nil {
		return 0, err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return 0, err
	}
	n, err := io.Copy(out, in)
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		_ = os.Remove(dst)
	}
	return n, err
}

func (b *Bootstrap) ready() {
	if b.ReadyFile != "" {
		_ = os.WriteFile(b.ReadyFile, nil, 0o644)
	}
}

func (b *Bootstrap) state(ctx context.Context) (string, error) {
	var st struct {
		State string `json:"state"`
	}
	code, err := b.call(ctx, "/v1/state", &st)
	if err != nil {
		return "", err
	}
	switch code {
	case http.StatusOK:
		return st.State, nil
	case http.StatusGone:
		return "", errOver
	default:
		return "", fmt.Errorf("fabric answered %d", code)
	}
}

func (b *Bootstrap) call(ctx context.Context, path string, out any) (int, error) {
	token, err := os.ReadFile(b.TokenFile)
	if err != nil {
		return 0, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimSuffix(b.FabricURL, "/")+path, nil)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(token)))
	resp, err := b.Client.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusOK && out != nil {
		if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
			return 0, err
		}
	}
	return resp.StatusCode, nil
}

// runAgent runs agent.jar until it exits or the controller's session changes.
func (b *Bootstrap) runAgent(ctx context.Context, bd Binding) error {
	session := b.session(ctx, bd.Controller)
	// The secret goes through a file, read once at start and removed: not on a command line
	// that every process in the sandbox can read in /proc.
	secretFile := filepath.Join(b.SecretDir, "agent-secret")
	if err := os.WriteFile(secretFile, []byte(bd.Secret), 0o600); err != nil {
		return err
	}
	cmd := exec.Command(b.Java, "-jar", b.AgentJar, "-url", bd.Controller, "-name", bd.Agent,
		"-secret", "@"+secretFile, "-webSocket", "-workDir", b.WorkDir)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		_ = os.Remove(secretFile)
		return err
	}
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()
	removeSecret := time.After(b.SecretTTL)
	t := time.NewTicker(b.SessionPoll)
	defer t.Stop()
	stop := func() error {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		select {
		case err := <-exited:
			return err
		case <-time.After(5 * time.Second):
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			return <-exited
		}
	}
	for {
		select {
		case err := <-exited:
			_ = os.Remove(secretFile)
			return fmt.Errorf("agent exited: %v", err)
		case <-ctx.Done():
			_ = os.Remove(secretFile)
			return stop()
		case <-removeSecret:
			_ = os.Remove(secretFile)
		case <-t.C:
			seen := b.session(ctx, bd.Controller)
			if session == "" {
				session = seen
				continue
			}
			if seen != "" && seen != session {
				b.Log.Warn("the controller was replaced: restarting the agent so it reconnects",
					"from", short(session), "to", short(seen))
				_ = os.Remove(secretFile)
				_ = stop()
				return errors.New("controller replaced")
			}
		}
	}
}

// session is the controller's X-Jenkins-Session, "" if it cannot be read now (a blip is not a
// new controller).
func (b *Bootstrap) session(ctx context.Context, controller string) string {
	cctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(cctx, http.MethodGet, strings.TrimSuffix(controller, "/")+"/login", nil)
	if err != nil {
		return ""
	}
	resp, err := b.Client.Do(req)
	if err != nil {
		return ""
	}
	resp.Body.Close()
	return resp.Header.Get("X-Jenkins-Session")
}

func short(s string) string {
	if len(s) > 8 {
		return s[:8]
	}
	return s
}
