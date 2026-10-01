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
	"sync/atomic"
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
	"github.com/erotonin/netci-delivery-platform/internal/kubeclient"
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
	// How long one API request may go unanswered before its connections are dropped and a read
	// is sent again (internal/kubeclient). Raise it only for an API server that is slow to start
	// answering the supervisor's lists.
	apiAttempt := duration("NETCI_API_ATTEMPT_TIMEOUT", 700*time.Millisecond, &errs)
	headroomEvery := duration("NETCI_HEADROOM_INTERVAL", 30*time.Second, &errs)
	if err := errors.Join(errs...); err != nil {
		return err
	}

	restCfg, err := rest.InClusterConfig()
	if err != nil {
		return err
	}
	// An observation that hangs is a gap, not a slow success. Short: the API server behind the
	// Service may be the one on the machine that just died (seen in the lab: 5 s timeouts made
	// the leader miss its own renewals on a healthy node).
	restCfg.Timeout = 2 * time.Second
	restCfg.QPS, restCfg.Burst = 50, 100
	// HTTP/1.1, a bound on each attempt, and every connection dropped when one fails: pooled
	// connections to the dead machine's API server failed two observations in a row in the lab,
	// the supervisor started over, and fenced at 9.7 s instead of ~4 s.
	// The API servers are dialled directly, avoiding for 30 s one that failed an attempt: through
	// the Service a new connection kept a one-in-three chance of the dead machine's until its
	// endpoint was removed (ADR-066).
	apiservers := kubeclient.NewBalancer(30 * time.Second)
	client, err := kubernetes.NewForConfig(kubeclient.Balanced(restCfg, apiAttempt, apiservers))
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
	go kubeclient.Discover(ctx, client, apiservers, 10*time.Second, log)

	for {
		err := supervisor.ValidateMachines(ctx, client, fencer, machines, 3*time.Second, 12*time.Second)
		var retry supervisor.Retryable
		if err == nil {
			break
		}
		if !errors.As(err, &retry) || ctx.Err() != nil {
			return err
		}
		log.Warn("startup check could not be completed; acting on nothing until it is", "error", err)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(5 * time.Second):
		}
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

	// Its own client for the leader lease: short calls, retried every second, so that losing a
	// connection does not cost the leadership at the moment the supervisor is needed.
	leCfg := rest.CopyConfig(restCfg)
	leCfg.Timeout = time.Second
	leClient, err := kubernetes.NewForConfig(kubeclient.Balanced(leCfg, min(apiAttempt, 500*time.Millisecond), apiservers))
	if err != nil {
		return err
	}
	lock := &resourcelock.LeaseLock{
		LeaseMeta:  metav1.ObjectMeta{Namespace: ns, Name: getenv("NETCI_SUPERVISOR_LEASE", "netci-supervisor")},
		Client:     leClient.CoordinationV1(),
		LockConfig: resourcelock.ResourceLockConfig{Identity: pod},
	}
	log.Info("supervisor starting", "identity", pod, "interval", interval, "node_stale", cfg.NodeStale,
		"auto_power_on", cfg.AutoPowerOn, "panic_fraction", cfg.PanicFraction)
	// Losing the leader lease (an API stall can cause it) puts the replica back into the
	// election instead of exiting: a restart cost 13 s in the lab. The supervisor's
	// observations start over in either case, so nothing seen before the loss is acted on.
	// Every replica observes; the leader acts. The observation runs for the process's whole life,
	// so a replica that becomes leader already knows how long each Lease has been unchanged.
	var leading atomic.Bool
	sup.Leading = leading.Load
	go sup.Run(ctx)
	// Whether each cell could be taken over if its machine were lost, checked ahead of the
	// loss: it lists every pod, so on its own client with room for a large cluster's answer.
	hrCfg := rest.CopyConfig(restCfg)
	hrCfg.Timeout = 20 * time.Second
	hrClient, err := kubernetes.NewForConfig(kubeclient.Balanced(hrCfg, 10*time.Second, apiservers))
	if err != nil {
		return err
	}
	go (&supervisor.HeadroomWatch{Client: hrClient, Interval: headroomEvery, Leading: leading.Load,
		Events: &supervisor.KubeEvents{Client: hrClient, Instance: pod, Log: log}, Metrics: metrics, Log: log}).Run(ctx)
	// A leader beside a cell's controller hands over to a replica on a machine without one, at
	// most once in 5 minutes (supervisor.YieldTo); with no replica there but a machine free, it
	// first has the standby recreated, at most once in 10 minutes (supervisor.Rebalance).
	var lastHandOver, lastRebalance time.Time
	handOver := func(ctx context.Context) (string, bool) {
		if time.Since(lastHandOver) < 5*time.Minute {
			return "", false
		}
		sups, err := client.CoreV1().Pods(ns).List(ctx, metav1.ListOptions{LabelSelector: "app=netci-supervisor"})
		if err != nil {
			return "", false
		}
		cells, err := client.CoreV1().Pods(metav1.NamespaceAll).List(ctx, metav1.ListOptions{LabelSelector: supervisor.CellLabel})
		if err != nil {
			return "", false
		}
		if to, ok := supervisor.YieldTo(pod, sups.Items, cells.Items); ok {
			return to, true
		}
		if time.Since(lastRebalance) < 10*time.Minute {
			return "", false
		}
		nodes, err := client.CoreV1().Nodes().List(ctx, metav1.ListOptions{})
		if err != nil {
			return "", false
		}
		if standby, ok := supervisor.Rebalance(pod, sups.Items, cells.Items, nodes.Items); ok {
			lastRebalance = time.Now()
			uid := standby.UID
			err := client.CoreV1().Pods(ns).Delete(ctx, standby.Name, metav1.DeleteOptions{Preconditions: &metav1.Preconditions{UID: &uid}})
			log.Info("recreating the standby replica to place it on a machine without a cell", "replica", standby.Name,
				"node", standby.Spec.NodeName, "error", err)
		}
		return "", false
	}
	for ctx.Err() == nil {
		if runElection(ctx, lock, &leading, metrics, log, handOver) {
			lastHandOver = time.Now()
			time.Sleep(3 * time.Second) // the other replica retries every second
			continue
		}
		if ctx.Err() == nil {
			log.Warn("lost the leader lease; standing for election again")
			time.Sleep(time.Second)
		}
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = server.Shutdown(shutdown)
	return nil
}

// runElection stands for election and acts while leading; it returns true when this replica
// handed its leadership over.
func runElection(ctx context.Context, lock resourcelock.Interface, leading *atomic.Bool, metrics *supervisor.Metrics, log *slog.Logger,
	handOver func(context.Context) (string, bool)) bool {
	ectx, release := context.WithCancel(ctx)
	defer release()
	var handed atomic.Bool
	leaderelection.RunOrDie(ectx, leaderelection.LeaderElectionConfig{
		// Short: when the leader dies with a cell's machine, this is added to the takeover.
		Lock: lock, LeaseDuration: 10 * time.Second, RenewDeadline: 7 * time.Second, RetryPeriod: time.Second,
		ReleaseOnCancel: true, Name: "netci-supervisor",
		Callbacks: leaderelection.LeaderCallbacks{
			OnStartedLeading: func(ctx context.Context) {
				leading.Store(true)
				metrics.SetLeading(true)
				log.Info("leading: acting on what this replica has been observing")
				t := time.NewTicker(15 * time.Second)
				defer t.Stop()
				for {
					select {
					case <-ctx.Done():
						return
					case <-t.C:
					}
					if to, ok := handOver(ctx); ok {
						log.Info("handing the leadership to a replica on a machine without a cell", "to", to)
						handed.Store(true)
						release() // ReleaseOnCancel: the lease is freed for the other replica at once
						return
					}
				}
			},
			OnStoppedLeading: func() {
				leading.Store(false)
				metrics.SetLeading(false)
				log.Info("no longer leading: observing only")
			},
		},
	})
	return handed.Load()
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
