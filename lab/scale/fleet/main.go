// Command fleet measures the Cell Supervisor at a size the lab's three machines cannot reach.
//
// Against a kwok cluster (a real API server, etcd, scheduler and controller manager, with nodes
// that only pretend to run pods) it creates NODES machines and CELLS cells, and stands in for
// what the lab has for real:
//
//   - each cell's agent: the real internal/lease.Holder, with the cell agent's timings, renews
//     the cell's Lease for as long as its pod runs on a live machine;
//
//   - each machine's BMC: a Redfish service answering for every machine, with TLS and basic auth,
//     as the supervisor's real Redfish client expects.
//
//   - each machine's kubelet heartbeat: its node Lease, renewed every 10 s for 40 s, as a kubelet
//     does (kwok runs with its own node leases off: kwok 0.8.0 kept renewing a node's Lease
//     after it stopped managing the node, so a machine could not be lost).
//
// It then runs the real supervisor binary against it, lets it settle, and powers machines off
// one at a time: the BMC says Off, the machine's heartbeats stop, its cells' agents stop.
// What it records: how long each observation took, how long each cell took to be held again,
// and what the supervisor cost (lab/scale/README.md).
//
//	go build -o /tmp/supervisor ./cmd/supervisor
//	go run ./lab/scale/fleet -kubeconfig kwok.kubeconfig -supervisor /tmp/supervisor -nodes 100 -cells 300 -kills 3
package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"math"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/clientcmd"
	"k8s.io/utils/clock"

	"github.com/erotonin/netci-delivery-platform/internal/lease"
)

const (
	kwokAnnotation = "kwok.x-k8s.io/node"
	bmcUser        = "fleet"
	bmcAddr        = "127.0.0.1:18443"
	supervisorNS   = "netci-system"
	supervisorHTTP = "127.0.0.1:19090"
)

type fleet struct {
	client *kubernetes.Clientset
	log    *slog.Logger
	dir    string

	mu      sync.Mutex
	power   map[string]bool               // node -> on
	holders map[string]context.CancelFunc // pod UID -> its agent
	podNode map[string]string             // pod UID -> node
	held    map[string]time.Time          // namespace -> when its current holder acquired the Lease
	holder  map[string]string             // namespace -> holder identity
}

func main() {
	kubeconfig := flag.String("kubeconfig", "", "the kwok cluster")
	supBin := flag.String("supervisor", "", "the supervisor binary (go build ./cmd/supervisor)")
	nodes := flag.Int("nodes", 100, "machines")
	cells := flag.Int("cells", 300, "cells")
	kills := flag.Int("kills", 3, "machines powered off, one after another")
	settle := flag.Duration("settle", 90*time.Second, "steady state measured before the first kill")
	out := flag.String("out", "lab/evidence", "where the evidence goes")
	flag.Parse()
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	if err := run(log, *kubeconfig, *supBin, *nodes, *cells, *kills, *settle, *out); err != nil {
		log.Error("fleet", "error", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger, kubeconfig, supBin string, nodes, cells, kills int, settle time.Duration, out string) error {
	cfg, err := clientcmd.BuildConfigFromFlags("", kubeconfig)
	if err != nil {
		return err
	}
	// Every agent renews once a second: the client must not be what limits them.
	cfg.QPS, cfg.Burst = 5000, 10000
	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		return err
	}
	dir, err := os.MkdirTemp("", "fleet-")
	if err != nil {
		return err
	}
	f := &fleet{client: client, log: log, dir: dir, power: map[string]bool{}, holders: map[string]context.CancelFunc{},
		podNode: map[string]string{}, held: map[string]time.Time{}, holder: map[string]string{}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	names, err := f.setup(ctx, nodes, cells)
	if err != nil {
		return err
	}
	if err := f.serveBMCs(ctx, names); err != nil {
		return err
	}
	go f.heartbeats(ctx, names)
	go f.agents(ctx)
	if err := waitFor(ctx, 10*time.Minute, func() bool { return f.heldCount() == cells }); err != nil {
		return fmt.Errorf("cells held before the supervisor: %d of %d: %w", f.heldCount(), cells, err)
	}
	log.Info("every cell held", "cells", cells)

	sup, err := f.startSupervisor(ctx, supBin, kubeconfig)
	if err != nil {
		return err
	}
	defer func() { _ = sup.Process.Kill(); _, _ = sup.Process.Wait() }()
	if err := waitFor(ctx, 2*time.Minute, func() bool { return metric(scrape(), "netci_supervisor_leading") == 1 }); err != nil {
		return fmt.Errorf("the supervisor never led: %w", err)
	}

	ev := evidence{Scenario: "scale", Nodes: nodes, Cells: cells, Started: time.Now().UTC().Format(time.RFC3339)}
	before := scrape()
	cpu0, wall0 := cpuSeconds(sup.Process.Pid), time.Now()
	time.Sleep(settle)
	steady := scrape()
	ev.Steady = observations(before, steady)
	ev.Steady.CPUCores = (cpuSeconds(sup.Process.Pid) - cpu0) / time.Since(wall0).Seconds()
	ev.Steady.RSSMiB = rssMiB(sup.Process.Pid)
	log.Info("steady state", "observations", ev.Steady)

	for k := 0; k < kills; k++ {
		r, err := f.kill(ctx)
		if err != nil {
			return err
		}
		ev.Kills = append(ev.Kills, r)
		log.Info("machine lost", "result", r)
	}
	ev.During = observations(steady, scrape())
	ev.SupervisorResets = metric(scrape(), "netci_supervisor_observation_resets_total")
	ev.SupervisorErrors = metric(scrape(), "netci_supervisor_observe_errors_total")
	ev.Verdict = "PASS"
	for _, k := range ev.Kills {
		if k.NotHeldAgain > 0 {
			ev.Verdict = "FAIL"
		}
	}
	name := filepath.Join(out, fmt.Sprintf("scale-%dnodes-%dcells-%s.json", nodes, cells, time.Now().UTC().Format("20060102T150405Z")))
	b, _ := json.MarshalIndent(ev, "", "  ")
	if err := os.WriteFile(name, append(b, '\n'), 0o644); err != nil {
		return err
	}
	fmt.Println(string(b))
	fmt.Println("evidence:", name)
	return nil
}

type evidence struct {
	Scenario         string       `json:"scenario"`
	Nodes            int          `json:"nodes"`
	Cells            int          `json:"cells"`
	Started          string       `json:"started"`
	Steady           observed     `json:"steady"`
	During           observed     `json:"duringKills"`
	Kills            []killResult `json:"kills"`
	SupervisorResets float64      `json:"supervisorObservationResets"`
	SupervisorErrors float64      `json:"supervisorObserveErrors"`
	Verdict          string       `json:"verdict"`
}

type observed struct {
	Count    float64 `json:"count"`
	MeanSec  float64 `json:"meanSeconds"`
	P50Sec   float64 `json:"p50Seconds"`
	P99Sec   float64 `json:"p99Seconds"`
	CPUCores float64 `json:"cpuCores,omitempty"`
	RSSMiB   float64 `json:"rssMiB,omitempty"`
}

type killResult struct {
	Node         string    `json:"node"`
	Cells        int       `json:"cells"`
	HeldAgainSec []float64 `json:"heldAgainSeconds"` // per cell, from the power loss
	NotHeldAgain int       `json:"notHeldAgain"`
	FencedSec    float64   `json:"nodeTaintedSeconds"`
	RecoveredSec float64   `json:"machineBackSeconds"`
}

// setup creates the machines and cells that are missing. Idempotent.
func (f *fleet) setup(ctx context.Context, nodes, cells int) ([]string, error) {
	var names []string
	for i := 0; i < nodes; i++ {
		name := fmt.Sprintf("fake-%03d", i)
		names = append(names, name)
		f.power[name] = true
		n := &corev1.Node{
			ObjectMeta: metav1.ObjectMeta{Name: name, Annotations: map[string]string{kwokAnnotation: "fake"},
				Labels: map[string]string{"kubernetes.io/hostname": name, "type": "kwok"}},
			Status: corev1.NodeStatus{Allocatable: resources("16", "64Gi", "110"), Capacity: resources("16", "64Gi", "110")},
		}
		if _, err := f.client.CoreV1().Nodes().Create(ctx, n, metav1.CreateOptions{}); err != nil && !apierrors.IsAlreadyExists(err) {
			return nil, err
		}
	}
	for _, ns := range []string{supervisorNS} {
		if _, err := f.client.CoreV1().Namespaces().Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}, metav1.CreateOptions{}); err != nil && !apierrors.IsAlreadyExists(err) {
			return nil, err
		}
	}
	one := int32(1)
	for i := 0; i < cells; i++ {
		ns := fmt.Sprintf("cell-%03d", i)
		if _, err := f.client.CoreV1().Namespaces().Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}, metav1.CreateOptions{}); err != nil && !apierrors.IsAlreadyExists(err) {
			return nil, err
		}
		labels := map[string]string{"app": "jenkins", "netci.io/cell": "true"}
		s := &appsv1.StatefulSet{
			ObjectMeta: metav1.ObjectMeta{Name: "jenkins", Namespace: ns, Labels: map[string]string{"netci.io/cell": "true"}},
			Spec: appsv1.StatefulSetSpec{Replicas: &one, ServiceName: "jenkins", Selector: &metav1.LabelSelector{MatchLabels: labels},
				Template: corev1.PodTemplateSpec{ObjectMeta: metav1.ObjectMeta{Labels: labels}, Spec: corev1.PodSpec{
					Affinity: &corev1.Affinity{NodeAffinity: &corev1.NodeAffinity{RequiredDuringSchedulingIgnoredDuringExecution: &corev1.NodeSelector{
						NodeSelectorTerms: []corev1.NodeSelectorTerm{{MatchExpressions: []corev1.NodeSelectorRequirement{{Key: "type", Operator: corev1.NodeSelectorOpIn, Values: []string{"kwok"}}}}}}}},
					Tolerations: []corev1.Toleration{{Key: "kwok.x-k8s.io/node", Operator: corev1.TolerationOpExists, Effect: corev1.TaintEffectNoSchedule}},
					Containers:  []corev1.Container{{Name: "jenkins", Image: "fake", Resources: corev1.ResourceRequirements{Requests: resources("1", "2Gi", "")}}},
				}}},
		}
		if _, err := f.client.AppsV1().StatefulSets(ns).Create(ctx, s, metav1.CreateOptions{}); err != nil && !apierrors.IsAlreadyExists(err) {
			return nil, err
		}
	}
	return names, nil
}

func resources(cpu, mem, pods string) corev1.ResourceList {
	r := corev1.ResourceList{corev1.ResourceCPU: resource.MustParse(cpu), corev1.ResourceMemory: resource.MustParse(mem)}
	if pods != "" {
		r[corev1.ResourcePods] = resource.MustParse(pods)
	}
	return r
}

// agents keeps one lease.Holder per cell pod that runs on a powered machine, as the cell agent
// beside a real controller would; an agent whose pod is gone or whose machine lost its power
// simply stops, as a real one would.
func (f *fleet) agents(ctx context.Context) {
	t := time.NewTicker(500 * time.Millisecond)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		pods, err := f.client.CoreV1().Pods(metav1.NamespaceAll).List(ctx, metav1.ListOptions{LabelSelector: "netci.io/cell=true"})
		if err != nil {
			continue
		}
		alive := map[string]bool{}
		f.mu.Lock()
		for i := range pods.Items {
			p := &pods.Items[i]
			uid := string(p.UID)
			if p.Status.Phase != corev1.PodRunning || p.DeletionTimestamp != nil || p.Spec.NodeName == "" || !f.power[p.Spec.NodeName] {
				continue
			}
			alive[uid] = true
			if _, ok := f.holders[uid]; ok {
				continue
			}
			hctx, stop := context.WithCancel(ctx)
			f.holders[uid], f.podNode[uid] = stop, p.Spec.NodeName
			ns, id := p.Namespace, p.Name+"/"+uid
			h := &lease.Holder{Leases: f.client.CoordinationV1(), Namespace: ns, Name: "jenkins", Identity: id,
				Duration: 15 * time.Second, RenewInterval: time.Second, RenewDeadline: 10 * time.Second, AttemptTimeout: 300 * time.Millisecond,
				Clock: clock.RealClock{}, Observer: lease.NewObserver(nil), Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
			h.OnAcquired = func(int32) {
				f.mu.Lock()
				f.held[ns], f.holder[ns] = time.Now(), id
				f.mu.Unlock()
			}
			go h.Run(hctx)
		}
		for uid, stop := range f.holders {
			if !alive[uid] || !f.power[f.podNode[uid]] {
				stop()
				delete(f.holders, uid)
				delete(f.podNode, uid)
			}
		}
		f.mu.Unlock()
	}
}

func (f *fleet) heldCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.held)
}

// kill powers off the machine running the most cells and waits for every one of them to be held
// again, then for the machine to be back (the supervisor runs with auto power-on).
func (f *fleet) kill(ctx context.Context) (killResult, error) {
	f.mu.Lock()
	per := map[string][]string{}
	for uid, node := range f.podNode {
		per[node] = append(per[node], uid)
	}
	var node string
	for n, uids := range per {
		if f.power[n] && (node == "" || len(uids) > len(per[node]) || len(uids) == len(per[node]) && n < node) {
			node = n
		}
	}
	victims := map[string]string{} // namespace -> holder at the loss
	for ns, id := range f.holder {
		uid := id[strings.Index(id, "/")+1:]
		if f.podNode[uid] == node {
			victims[ns] = id
		}
	}
	f.power[node] = false // the BMC says Off; its heartbeats and its agents stop
	// At once: an agent left to the next pass could renew after the power was gone, which a dead
	// machine cannot do. The supervisor then (rightly) does not release a Lease renewed after its
	// machine was off, and waits out its expiry: the first runs had one cell held 15 s late.
	for uid, n := range f.podNode {
		if n == node {
			f.holders[uid]()
			delete(f.holders, uid)
			delete(f.podNode, uid)
		}
	}
	f.mu.Unlock()
	lost := time.Now()
	r := killResult{Node: node, Cells: len(victims)}
	deadline := time.Now().Add(5 * time.Minute)
	for time.Now().Before(deadline) {
		if r.FencedSec == 0 && f.tainted(ctx, node) {
			r.FencedSec = round(time.Since(lost).Seconds())
		}
		f.mu.Lock()
		done := 0
		for ns, old := range victims {
			if f.holder[ns] != old {
				done++
			}
		}
		f.mu.Unlock()
		if done == len(victims) {
			break
		}
		time.Sleep(200 * time.Millisecond)
	}
	f.mu.Lock()
	for ns, old := range victims {
		if f.holder[ns] == old {
			r.NotHeldAgain++
			continue
		}
		r.HeldAgainSec = append(r.HeldAgainSec, round(f.held[ns].Sub(lost).Seconds()))
	}
	f.mu.Unlock()
	sort.Float64s(r.HeldAgainSec)
	// The supervisor powers the machine back on once its cells are held again; its heartbeats
	// start again, and the supervisor removes its taint once it is Ready.
	_ = waitFor(ctx, 5*time.Minute, func() bool { return !f.tainted(ctx, node) && f.isOn(node) })
	r.RecoveredSec = round(time.Since(lost).Seconds())
	return r, nil
}

func (f *fleet) isOn(node string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.power[node]
}

func (f *fleet) tainted(ctx context.Context, node string) bool {
	n, err := f.client.CoreV1().Nodes().Get(ctx, node, metav1.GetOptions{})
	if err != nil {
		return false
	}
	for _, t := range n.Spec.Taints {
		if t.Key == "node.kubernetes.io/out-of-service" {
			return true
		}
	}
	return false
}

// heartbeats renews the node Lease of every powered machine, as its kubelet would.
func (f *fleet) heartbeats(ctx context.Context, nodes []string) {
	duration := int32(40)
	t := time.NewTicker(10 * time.Second)
	defer t.Stop()
	for {
		for _, n := range nodes {
			if !f.isOn(n) {
				continue
			}
			now := metav1.NewMicroTime(time.Now())
			leases := f.client.CoordinationV1().Leases("kube-node-lease")
			l, err := leases.Get(ctx, n, metav1.GetOptions{})
			switch {
			case apierrors.IsNotFound(err):
				id := n
				_, err = leases.Create(ctx, &coordinationv1.Lease{ObjectMeta: metav1.ObjectMeta{Name: n},
					Spec: coordinationv1.LeaseSpec{HolderIdentity: &id, LeaseDurationSeconds: &duration, RenewTime: &now}}, metav1.CreateOptions{})
			case err == nil:
				l.Spec.RenewTime = &now
				_, err = leases.Update(ctx, l, metav1.UpdateOptions{})
			}
			if err != nil && ctx.Err() == nil {
				f.log.Warn("node heartbeat", "node", n, "error", err)
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// serveBMCs answers Redfish for every machine and writes the supervisor's fence.json.
func (f *fleet) serveBMCs(ctx context.Context, nodes []string) error {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return err
	}
	tmpl := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "fleet-bmc"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(24 * time.Hour), IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(der)
	password := hex.EncodeToString(sum[:8]) // a throwaway secret for a throwaway server
	creds := filepath.Join(f.dir, "bmc")
	if err := os.MkdirAll(creds, 0o700); err != nil {
		return err
	}
	for name, v := range map[string]string{"username": bmcUser, "password": password, "tls-sha256": hex.EncodeToString(sum[:])} {
		if err := os.WriteFile(filepath.Join(creds, name), []byte(v), 0o600); err != nil {
			return err
		}
	}
	cfg := map[string]any{}
	for _, n := range nodes {
		cfg[n] = map[string]any{"redfish": map[string]any{"endpoint": "https://" + bmcAddr, "system": "/redfish/v1/Systems/" + n, "credentials": creds}}
	}
	b, _ := json.Marshal(map[string]any{"nodes": cfg})
	if err := os.WriteFile(filepath.Join(f.dir, "fence.json"), b, 0o600); err != nil {
		return err
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/redfish/v1/Systems/", func(w http.ResponseWriter, r *http.Request) {
		if u, p, ok := r.BasicAuth(); !ok || u != bmcUser || p != password {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		rest := strings.TrimPrefix(r.URL.Path, "/redfish/v1/Systems/")
		node, action, _ := strings.Cut(rest, "/")
		f.mu.Lock()
		on, known := f.power[node]
		f.mu.Unlock()
		if !known {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		switch {
		case r.Method == http.MethodGet && action == "":
			state := map[bool]string{true: "On", false: "Off"}[on]
			_ = json.NewEncoder(w).Encode(map[string]any{"PowerState": state, "Actions": map[string]any{"#ComputerSystem.Reset": map[string]any{
				"target": "/redfish/v1/Systems/" + node + "/Actions/ComputerSystem.Reset", "ResetType@Redfish.AllowableValues": []string{"On", "ForceOff"}}}})
		case r.Method == http.MethodPost && action == "Actions/ComputerSystem.Reset":
			var body struct{ ResetType string }
			_ = json.NewDecoder(r.Body).Decode(&body)
			switch body.ResetType {
			case "ForceOff":
				f.mu.Lock()
				f.power[node] = false
				f.mu.Unlock()
			case "On":
				f.mu.Lock()
				f.power[node] = true // its heartbeats start again
				f.mu.Unlock()
			default:
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			w.WriteHeader(http.StatusNoContent)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	})
	ln, err := net.Listen("tcp", bmcAddr)
	if err != nil {
		return err
	}
	srv := &http.Server{Handler: mux, TLSConfig: &tls.Config{Certificates: []tls.Certificate{{Certificate: [][]byte{der}, PrivateKey: key}}},
		ReadHeaderTimeout: 5 * time.Second}
	go func() { _ = srv.ServeTLS(ln, "", "") }()
	go func() { <-ctx.Done(); _ = srv.Close() }()
	return nil
}

func (f *fleet) startSupervisor(ctx context.Context, bin, kubeconfig string) (*exec.Cmd, error) {
	cmd := exec.CommandContext(ctx, bin)
	cmd.Env = append(os.Environ(), "KUBECONFIG="+kubeconfig, "POD_NAME=supervisor-0", "POD_NAMESPACE="+supervisorNS,
		"NETCI_FENCE_CONFIG="+filepath.Join(f.dir, "fence.json"), "NETCI_AUTO_POWER_ON=true", "NETCI_HTTP_ADDR="+supervisorHTTP)
	logFile, err := os.Create(filepath.Join(f.dir, "supervisor.log"))
	if err != nil {
		return nil, err
	}
	cmd.Stdout, cmd.Stderr = logFile, logFile
	f.log.Info("starting the supervisor", "log", logFile.Name())
	return cmd, cmd.Start()
}

// scrape reads the supervisor's metrics: name{labels} -> value.
func scrape() map[string]float64 {
	out := map[string]float64{}
	resp, err := http.Get("http://" + supervisorHTTP + "/metrics")
	if err != nil {
		return out
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	for _, line := range strings.Split(string(b), "\n") {
		if line == "" || line[0] == '#' {
			continue
		}
		i := strings.LastIndex(line, " ")
		if v, err := strconv.ParseFloat(line[i+1:], 64); err == nil {
			out[line[:i]] = v
		}
	}
	return out
}

func metric(m map[string]float64, name string) float64 { return m[name] }

// observations: the observation-time histogram between two scrapes.
func observations(a, b map[string]float64) observed {
	const h = "netci_supervisor_observation_seconds"
	count := b[h+"_count"] - a[h+"_count"]
	o := observed{Count: count}
	if count == 0 {
		return o
	}
	o.MeanSec = math.Round((b[h+"_sum"]-a[h+"_sum"])/count*1000) / 1000
	type bucket struct{ le, n float64 }
	var bs []bucket
	for k, v := range b {
		if strings.HasPrefix(k, h+"_bucket{le=") {
			le, _ := strconv.ParseFloat(strings.Trim(strings.TrimPrefix(k, h+"_bucket{le="), `"}`), 64)
			bs = append(bs, bucket{le, v - a[k]})
		}
	}
	sort.Slice(bs, func(i, j int) bool { return bs[i].le < bs[j].le })
	q := func(p float64) float64 {
		for _, x := range bs {
			if x.n >= p*count {
				return x.le
			}
		}
		return math.Inf(1)
	}
	o.P50Sec, o.P99Sec = q(0.5), q(0.99)
	return o
}

func cpuSeconds(pid int) float64 {
	b, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return 0
	}
	f := strings.Fields(string(b)[strings.LastIndex(string(b), ")")+2:])
	ut, _ := strconv.ParseFloat(f[11], 64)
	st, _ := strconv.ParseFloat(f[12], 64)
	return (ut + st) / 100 // USER_HZ
}

func rssMiB(pid int) float64 {
	b, err := os.ReadFile(fmt.Sprintf("/proc/%d/status", pid))
	if err != nil {
		return 0
	}
	for _, l := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(l, "VmRSS:") {
			kb, _ := strconv.ParseFloat(strings.Fields(l)[1], 64)
			return round(kb / 1024)
		}
	}
	return 0
}

func waitFor(ctx context.Context, d time.Duration, ok func() bool) error {
	deadline := time.Now().Add(d)
	for !ok() {
		if time.Now().After(deadline) {
			return fmt.Errorf("timed out after %s", d)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(500 * time.Millisecond):
		}
	}
	return nil
}

func round(x float64) float64 { return math.Round(x*10) / 10 }
