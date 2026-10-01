// Command cell-agent is the sidecar of a Jenkins cell (ADR-060): it holds the cell's Lease and
// lets the controller run only while it does. See internal/cellagent.
//
//	cell-agent                      the sidecar
//	cell-agent guard -- CMD ARGS    the controller container's command: runs CMD behind the gate
//	cell-agent install DIR          copies this binary into DIR (an init container uses it to
//	                                hand the guard to the controller's image, which has no copy)
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/utils/clock"

	"github.com/erotonin/netci-delivery-platform/internal/cellagent"
	"github.com/erotonin/netci-delivery-platform/internal/lease"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if len(os.Args) > 1 && os.Args[1] == "guard" {
		os.Exit(guard(log, os.Args[2:]))
	}
	if len(os.Args) == 3 && os.Args[1] == "install" {
		if err := install(os.Args[2]); err != nil {
			log.Error("install", "error", err)
			os.Exit(1)
		}
		return
	}
	if err := run(log); err != nil {
		log.Error("cell-agent stopped", "error", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	pod, uid, ns := os.Getenv("POD_NAME"), os.Getenv("POD_UID"), os.Getenv("POD_NAMESPACE")
	if pod == "" || uid == "" || ns == "" {
		return errors.New("POD_NAME, POD_UID and POD_NAMESPACE must come from the downward API")
	}
	duration, err := env("NETCI_LEASE_DURATION", 6*time.Second)
	if err != nil {
		return err
	}
	interval, err := env("NETCI_RENEW_INTERVAL", time.Second)
	if err != nil {
		return err
	}
	deadline, err := env("NETCI_RENEW_DEADLINE", 4*time.Second)
	if err != nil {
		return err
	}
	grace, err := env("NETCI_TERMINATION_GRACE", 30*time.Second)
	if err != nil {
		return err
	}

	cfg, err := rest.InClusterConfig()
	if err != nil {
		return err
	}
	// A renewal that waits on a dead connection must not outlast the renew deadline; the holder
	// also bounds each call, this bounds the transport underneath it.
	cfg.Timeout = interval
	// HTTP/1.1: a request that times out closes its connection, and the next one is balanced
	// afresh. One HTTP/2 connection to an API server on a dead machine would carry every
	// renewal into the void until its health check notices (45 s by default).
	cfg.TLSClientConfig.NextProtos = []string{"http/1.1"}
	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		return err
	}

	registry := prometheus.NewRegistry()
	holder := &lease.Holder{
		Leases: client.CoordinationV1(), Namespace: ns, Name: getenv("NETCI_LEASE_NAME", "netci-cell"),
		Identity: pod + "/" + uid, Duration: duration, RenewInterval: interval, RenewDeadline: deadline,
		AttemptTimeout: 300 * time.Millisecond,
		Clock:          clock.RealClock{}, Observer: lease.NewObserver(nil), Log: log,
	}
	if err := holder.Validate(); err != nil {
		return err
	}
	agent := &cellagent.Agent{
		Holder: holder, GateFile: getenv("NETCI_GATE_FILE", "/run/netci/gate"),
		Procs: cellagent.NewProcesses(getenv("NETCI_CONTROLLER_MATCH", "jenkins.war")),
		Log:   log, KeepDeadEvery: 500 * time.Millisecond, Metrics: cellagent.NewMetrics(registry),
	}
	agent.Wire()

	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		if held, epoch := holder.Holding(); held {
			fmt.Fprintf(w, "holding epoch %d\n", epoch)
			return
		}
		http.Error(w, "not holding the cell lease", http.StatusServiceUnavailable)
	})
	server := &http.Server{Addr: getenv("NETCI_HTTP_ADDR", ":9090"), Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("http server", "error", err)
		}
	}()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	log.Info("cell-agent starting", "identity", holder.Identity, "lease", ns+"/"+holder.Name,
		"duration", duration, "interval", interval, "deadline", deadline)
	agent.Run(ctx) // returns when SIGTERM arrives
	log.Info("termination: keeping the lease until the controller has exited", "grace", grace)
	shutdownCtx, cancel := context.WithTimeout(context.Background(), grace+5*time.Second)
	defer cancel()
	agent.Shutdown(shutdownCtx, grace)
	_ = server.Shutdown(shutdownCtx)
	return nil
}

func guard(log *slog.Logger, args []string) int {
	if len(args) < 2 || args[0] != "--" {
		log.Error("usage: cell-agent guard -- COMMAND [ARGS]")
		return 2
	}
	stale, err := env("NETCI_RENEW_DEADLINE", 4*time.Second)
	if err != nil {
		log.Error("bad configuration", "error", err)
		return 2
	}
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	g := &cellagent.Guard{
		GateFile: getenv("NETCI_GATE_FILE", "/run/netci/gate"), Stale: stale, Poll: 250 * time.Millisecond,
		Command: args[1:], Log: log.With("component", "guard"), Now: time.Now,
	}
	code, err := g.Run(context.Background(), signals)
	if err != nil {
		log.Error("guard", "error", err, "exit", code)
	}
	return code
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
	dst := filepath.Join(dir, "cell-agent")
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

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func env(key string, def time.Duration) (time.Duration, error) {
	v := os.Getenv(key)
	if v == "" {
		return def, nil
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return 0, fmt.Errorf("%s: %w", key, err)
	}
	return d, nil
}
