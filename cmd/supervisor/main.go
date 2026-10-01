// Command supervisor watches every cell's Lease, fences the machine of a controller that stopped
// renewing, and lets Kubernetes start it elsewhere (ADR-060). See internal/supervisor.
//
// Several replicas may run; one acts, chosen by a Lease of its own.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"golang.org/x/crypto/ssh"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/leaderelection"
	"k8s.io/client-go/tools/leaderelection/resourcelock"
	"k8s.io/utils/clock"

	"github.com/erotonin/netci-delivery-platform/internal/fence"
	"github.com/erotonin/netci-delivery-platform/internal/supervisor"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(log); err != nil {
		log.Error("supervisor stopped", "error", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	pod, ns := os.Getenv("POD_NAME"), os.Getenv("POD_NAMESPACE")
	if pod == "" || ns == "" {
		return errors.New("POD_NAME and POD_NAMESPACE must come from the downward API")
	}
	fencer, machines, err := powerControllers()
	if err != nil {
		return err
	}
	cfg := supervisor.DefaultConfig()
	var errs []error
	cfg.NodeStale = duration("NETCI_NODE_STALE", cfg.NodeStale, &errs)
	cfg.StuckPodAfter = duration("NETCI_STUCK_POD_AFTER", cfg.StuckPodAfter, &errs)
	cfg.Cooldown = duration("NETCI_COOLDOWN", cfg.Cooldown, &errs)
	cfg.PowerOnAfter = duration("NETCI_POWER_ON_AFTER", cfg.PowerOnAfter, &errs)
	cfg.AutoPowerOn = os.Getenv("NETCI_AUTO_POWER_ON") == "true"
	if v := os.Getenv("NETCI_PANIC_FRACTION"); v != "" {
		f, err := strconv.ParseFloat(v, 64)
		if err != nil || f <= 0 || f > 1 {
			errs = append(errs, fmt.Errorf("NETCI_PANIC_FRACTION: want (0,1], got %q", v))
		}
		cfg.PanicFraction = f
	}
	interval := duration("NETCI_INTERVAL", time.Second, &errs)
	offTimeout := duration("NETCI_OFF_TIMEOUT", 20*time.Second, &errs)
	if err := errors.Join(errs...); err != nil {
		return err
	}

	restCfg, err := rest.InClusterConfig()
	if err != nil {
		return err
	}
	restCfg.Timeout = 5 * time.Second // an observation that hangs is a gap, not a slow success
	restCfg.QPS, restCfg.Burst = 50, 100
	client, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		return err
	}

	registry := prometheus.NewRegistry()
	registry.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	metrics := supervisor.NewMetrics(registry)
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	server := &http.Server{Addr: getenv("NETCI_HTTP_ADDR", ":9090"), Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("http server", "error", err)
		}
	}()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	if err := supervisor.ValidateMachines(ctx, client, fencer, machines, 5*time.Second); err != nil {
		return err
	}
	log.Info("node-to-machine mapping checked against the power controller", "machines", len(machines), "fencer", fencer.Name())

	sup := &supervisor.Supervisor{
		Collector: &supervisor.Collector{Client: client, Leases: client.CoordinationV1(), Fencer: fencer, Machines: machines,
			Config: cfg, Clock: clock.RealClock{}, MaxGap: 3 * interval, StateTimeout: 3 * time.Second},
		Executor: &supervisor.Executor{Client: client, Leases: client.CoordinationV1(), Fencer: fencer, Clock: clock.RealClock{},
			Log: log, Events: &supervisor.KubeEvents{Client: client, Instance: pod, Log: log}, Metrics: metrics, OffTimeout: offTimeout},
		Config: cfg, Interval: interval, ActionTimeout: offTimeout + 30*time.Second, AlertEvery: time.Minute,
		Clock: clock.RealClock{}, Log: log, Metrics: metrics,
	}

	lock := &resourcelock.LeaseLock{
		LeaseMeta:  metav1.ObjectMeta{Namespace: ns, Name: getenv("NETCI_SUPERVISOR_LEASE", "netci-supervisor")},
		Client:     client.CoordinationV1(),
		LockConfig: resourcelock.ResourceLockConfig{Identity: pod},
	}
	log.Info("supervisor starting", "identity", pod, "interval", interval, "node_stale", cfg.NodeStale,
		"auto_power_on", cfg.AutoPowerOn, "panic_fraction", cfg.PanicFraction)
	leaderelection.RunOrDie(ctx, leaderelection.LeaderElectionConfig{
		Lock: lock, LeaseDuration: 15 * time.Second, RenewDeadline: 10 * time.Second, RetryPeriod: 2 * time.Second,
		ReleaseOnCancel: true, Name: "netci-supervisor",
		Callbacks: leaderelection.LeaderCallbacks{
			OnStartedLeading: func(ctx context.Context) {
				metrics.SetLeading(true)
				log.Info("leading: observing cells")
				sup.Run(ctx)
			},
			OnStoppedLeading: func() {
				metrics.SetLeading(false)
				log.Info("no longer leading")
			},
		},
	})
	shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = server.Shutdown(shutdown)
	if ctx.Err() == nil {
		// Lost the leadership without being asked to stop: exit, and start over with nothing
		// remembered rather than act on observations from before the loss.
		return errors.New("lost the leader lease")
	}
	return nil
}

// powerControllers reads NETCI_FENCE_CONFIG (one power controller per node: Redfish BMCs, the
// lab's SSH agent; see internal/fence.Config) or, for the lab, NETCI_MACHINES with one SSH agent.
func powerControllers() (fence.Fencer, map[string]string, error) {
	if path := os.Getenv("NETCI_FENCE_CONFIG"); path != "" {
		return fence.LoadConfig(path)
	}
	machines, err := supervisor.ParseMachines(os.Getenv("NETCI_MACHINES"))
	if err != nil {
		return nil, nil, err
	}
	f, err := sshFencer()
	if err != nil {
		return nil, nil, err
	}
	return f, machines, nil
}

// sshFencer builds the power controller client. The key and the host key come from files
// mounted from a Secret; neither is ever logged.
func sshFencer() (*fence.SSH, error) {
	addr, user := os.Getenv("NETCI_FENCE_ADDR"), getenv("NETCI_FENCE_USER", "netci-fence")
	keyFile, hostKeyFile := getenv("NETCI_FENCE_KEY", "/etc/netci/fence/id_ed25519"), getenv("NETCI_FENCE_HOST_KEY", "/etc/netci/fence/host_key.pub")
	if addr == "" {
		return nil, errors.New("NETCI_FENCE_ADDR is required: without a power controller nothing can be fenced")
	}
	pem, err := os.ReadFile(keyFile)
	if err != nil {
		return nil, fmt.Errorf("fence key: %w", err)
	}
	signer, err := ssh.ParsePrivateKey(pem)
	if err != nil {
		return nil, fmt.Errorf("fence key %s: not a usable private key", keyFile)
	}
	hk, err := os.ReadFile(hostKeyFile)
	if err != nil {
		return nil, fmt.Errorf("fence host key: %w", err)
	}
	hostKey, _, _, _, err := ssh.ParseAuthorizedKey(hk)
	if err != nil {
		return nil, fmt.Errorf("fence host key %s: want one public key line (\"ssh-ed25519 AAAA...\"): %w", hostKeyFile, err)
	}
	return &fence.SSH{Addr: addr, User: user, Signer: signer, HostKey: hostKey, Timeout: 3 * time.Second}, nil
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func duration(key string, def time.Duration, errs *[]error) time.Duration {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	d, err := time.ParseDuration(v)
	if err != nil || d <= 0 {
		*errs = append(*errs, fmt.Errorf("%s: want a positive duration, got %q", key, v))
		return def
	}
	return d
}
