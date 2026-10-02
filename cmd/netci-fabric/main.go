// Command netci-fabric keeps warm build sandboxes and binds them to the controllers that claim
// them (ADR-064). Several replicas may run; the one holding the leader lease serves and
// reconciles, and points the Service's EndpointSlice at itself, so traffic reaches it alone.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/leaderelection"
	"k8s.io/client-go/tools/leaderelection/resourcelock"
	"k8s.io/utils/clock"

	"github.com/erotonin/netci-delivery-platform/internal/fabric"
	"github.com/erotonin/netci-delivery-platform/internal/kubeclient"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(log); err != nil {
		log.Error("netci-fabric stopped", "error", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	pod, ns, ip, dsn := os.Getenv("POD_NAME"), os.Getenv("POD_NAMESPACE"), os.Getenv("POD_IP"), os.Getenv("DATABASE_URL")
	if pod == "" || ns == "" || ip == "" || dsn == "" {
		return errors.New("POD_NAME, POD_NAMESPACE, POD_IP and DATABASE_URL are required")
	}
	cfg, err := fabric.LoadConfig(getenv("NETCI_FABRIC_CONFIG", "/etc/netci/fabric/config.json"))
	if err != nil {
		return err
	}
	restCfg, err := rest.InClusterConfig()
	if err != nil {
		return err
	}
	restCfg.Timeout = 10 * time.Second
	// See internal/kubeclient: no request waits 10 s on a connection to a dead machine.
	client, err := kubernetes.NewForConfig(kubeclient.Config(restCfg, 2*time.Second))
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return fmt.Errorf("database: %w", err)
	}
	defer pool.Close()
	if err := fabric.Migrate(ctx, pool); err != nil {
		return fmt.Errorf("migrate: %w", err)
	}

	registry := prometheus.NewRegistry()
	registry.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	metrics := fabric.NewMetrics(registry)
	pools := make([]string, 0, len(cfg.Pools))
	for _, p := range cfg.Pools {
		pools = append(pools, p.Name)
	}
	metrics.Known(pools)
	store := &fabric.Store{Pool: pool}
	bindings := fabric.NewBindings()
	recon := &fabric.Reconciler{Store: store, Client: client, Pods: cfg.Settings(), Pools: cfg.PoolMap(), Bindings: bindings,
		Clock: clock.RealClock{}, Log: log, Metrics: metrics, BindTimeout: 2 * time.Minute, StartTimeout: 5 * time.Minute}
	api := &fabric.API{Store: store, Client: client, Pods: cfg.Settings(), Pools: cfg.PoolMap(), Cells: cfg.Cells,
		Bindings: bindings, Recon: recon, Log: log, Metrics: metrics, LongPoll: 30 * time.Second}

	var leading atomic.Bool
	mux := http.NewServeMux()
	apiHandler := api.Handler()
	mux.Handle("/v1/", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !leading.Load() {
			http.Error(w, "not the leader", http.StatusServiceUnavailable)
			return
		}
		apiHandler.ServeHTTP(w, r)
	}))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.Handle("GET /metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))
	server := &http.Server{Addr: getenv("NETCI_HTTP_ADDR", ":8080"), Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("http server", "error", err)
			stop()
		}
	}()

	lock := &resourcelock.LeaseLock{LeaseMeta: metav1.ObjectMeta{Namespace: ns, Name: "netci-fabric"},
		Client: client.CoordinationV1(), LockConfig: resourcelock.ResourceLockConfig{Identity: pod}}
	leaderelection.RunOrDie(ctx, leaderelection.LeaderElectionConfig{
		Lock: lock, LeaseDuration: 15 * time.Second, RenewDeadline: 10 * time.Second, RetryPeriod: 2 * time.Second,
		ReleaseOnCancel: true, Name: "netci-fabric",
		Callbacks: leaderelection.LeaderCallbacks{
			OnStartedLeading: func(ctx context.Context) {
				leading.Store(true)
				if err := fabric.PublishLeader(ctx, client, ns, ip); err != nil {
					log.Error("cannot route the Service to this replica; giving up leadership", "error", err)
					return
				}
				log.Info("leading: reconciling sandboxes", "pools", len(cfg.Pools))
				t := time.NewTicker(time.Second)
				defer t.Stop()
				for i := 1; ; i++ {
					if err := recon.Tick(ctx); err != nil && ctx.Err() == nil {
						log.Error("reconcile", "error", err)
					}
					// Again every 15 s: anything that rewrites the slice (a helm upgrade renders it
					// empty) would otherwise cut the Service off from the leader until it changes.
					if i%15 == 0 {
						if err := fabric.PublishLeader(ctx, client, ns, ip); err != nil && ctx.Err() == nil {
							log.Error("routing the Service to this replica", "error", err)
						}
					}
					select {
					case <-ctx.Done():
						return
					case <-t.C:
					}
				}
			},
			OnStoppedLeading: func() { leading.Store(false) },
		},
	})
	shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = server.Shutdown(shutdown)
	if ctx.Err() == nil {
		return errors.New("lost the leader lease") // start over: the bindings in memory are gone
	}
	return nil
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
