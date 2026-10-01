// Command netci-sandbox is a build sandbox's entrypoint (ADR-064): it waits, warm, for the
// fabric to bind it, runs the Jenkins agent, and stops when its build is over.
//
//	netci-sandbox              run (environment: NETCI_FABRIC_URL, NETCI_FABRIC_TOKEN, NETCI_AGENT_JAR, NETCI_WORKDIR, NETCI_READY_FILE)
//	netci-sandbox ready        readiness probe: exits 0 once the fabric has answered
//	netci-sandbox install DIR  copies this binary into DIR (an init container hands it to the agent image)
package main

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/erotonin/netci-delivery-platform/internal/sandbox"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil)).With("component", "netci-sandbox")
	if len(os.Args) == 2 && os.Args[1] == "ready" {
		if _, err := os.Stat(getenv("NETCI_READY_FILE", "/tmp/netci-ready")); err != nil {
			os.Exit(1)
		}
		return
	}
	if len(os.Args) == 3 && os.Args[1] == "install" {
		if err := install(os.Args[2]); err != nil {
			log.Error("install", "error", err)
			os.Exit(1)
		}
		return
	}
	b := &sandbox.Bootstrap{
		FabricURL: os.Getenv("NETCI_FABRIC_URL"), TokenFile: getenv("NETCI_FABRIC_TOKEN", "/var/run/secrets/netci/token"),
		Java: getenv("NETCI_JAVA", "java"), AgentJar: getenv("NETCI_AGENT_JAR", "/usr/share/jenkins/agent.jar"),
		WorkDir: getenv("NETCI_WORKDIR", "/home/jenkins/agent"), SecretDir: getenv("NETCI_SECRET_DIR", "/tmp"),
		ReadyFile: getenv("NETCI_READY_FILE", "/tmp/netci-ready"),
		// Longer than the fabric's long poll.
		Client: &http.Client{Timeout: 45 * time.Second}, Log: log,
		SessionPoll: 2 * time.Second, Retry: 2 * time.Second, SecretTTL: 10 * time.Second,
	}
	if b.FabricURL == "" {
		log.Error("NETCI_FABRIC_URL is required")
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	if err := b.Run(ctx); err != nil {
		log.Error("sandbox stopped", "error", err)
		os.Exit(1)
	}
}

func install(dir string) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	in, err := os.Open(self)
	if err != nil {
		return err
	}
	defer in.Close()
	dst := filepath.Join(dir, "netci-sandbox")
	out, err := os.OpenFile(dst+".tmp", os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o555)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	return os.Rename(dst+".tmp", dst)
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
