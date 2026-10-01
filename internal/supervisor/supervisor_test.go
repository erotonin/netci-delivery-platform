package supervisor

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	appsv1 "k8s.io/api/apps/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	k8stesting "k8s.io/client-go/testing"
	clocktesting "k8s.io/utils/clock/testing"

	"github.com/erotonin/netci-delivery-platform/internal/fence"
	"github.com/erotonin/netci-delivery-platform/internal/lease"
	"github.com/erotonin/netci-delivery-platform/internal/lease/leasetest"
)

// lab is a three-node cluster with one cell, a power controller, and a log of every operation
// that matters to safety, in the order it happened.
type lab struct {
	t       *testing.T
	clk     *clocktesting.FakeClock
	kube    *fake.Clientset
	leases  *leasetest.APIServer
	power   *fakePower
	events  *fakeEvents
	sup     *Supervisor
	metrics *Metrics

	mu        sync.Mutex
	ops       []string
	apiDown   bool
	alive     map[string]bool // node -> kubelet heartbeating
	cellAlive bool            // the controller pod renews its lease
}

const holder = "jenkins-0/uid-1"

func newLab(t *testing.T) *lab {
	l := &lab{t: t, clk: clocktesting.NewFakeClock(time.Unix(1_000_000, 0)), leases: leasetest.New(),
		alive: map[string]bool{"netci-lab-1": true, "netci-lab-2": true, "netci-lab-3": true}, cellAlive: true}
	l.power = &fakePower{states: map[string]fence.State{}, lab: l}
	l.events = &fakeEvents{}
	objs := []runtime.Object{
		&appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: "jenkins", Namespace: "cell-a", Labels: map[string]string{CellLabel: "true"}}},
		&corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "jenkins-0", Namespace: "cell-a", UID: "uid-1", Labels: map[string]string{CellLabel: "true"}},
			Spec: corev1.PodSpec{NodeName: "netci-lab-1"}},
		// Storage's own pod on the same machine, and one on another machine.
		&corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "longhorn-manager-a", Namespace: "longhorn-system", UID: "uid-lh-a"},
			Spec: corev1.PodSpec{NodeName: "netci-lab-1"}},
		&corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "longhorn-manager-b", Namespace: "longhorn-system", UID: "uid-lh-b"},
			Spec: corev1.PodSpec{NodeName: "netci-lab-2"}},
	}
	for name := range l.alive {
		l.power.states[name] = fence.Running
		objs = append(objs, &corev1.Node{
			ObjectMeta: metav1.ObjectMeta{Name: name, Labels: map[string]string{controlPlaneLabel: "true"}},
			Status:     corev1.NodeStatus{Conditions: []corev1.NodeCondition{{Type: corev1.NodeReady, Status: corev1.ConditionTrue}}},
		})
		l.create("kube-node-lease", name, name)
	}
	l.create("cell-a", "jenkins", holder)
	l.kube = fake.NewClientset(objs...)
	l.kube.PrependReactor("*", "*", func(a k8stesting.Action) (bool, runtime.Object, error) {
		l.mu.Lock()
		defer l.mu.Unlock()
		if l.apiDown {
			return true, nil, errors.New("dial tcp: i/o timeout")
		}
		switch {
		case a.GetVerb() == "update" && a.GetResource().Resource == "nodes" && a.GetSubresource() == "status":
			n := a.(k8stesting.UpdateAction).GetObject().(*corev1.Node)
			for _, c := range n.Status.Conditions {
				if c.Type == corev1.NodeReady && c.Status != corev1.ConditionTrue {
					l.ops = append(l.ops, "not-ready "+n.Name)
				}
			}
		case a.GetVerb() == "update" && a.GetResource().Resource == "nodes":
			n := a.(k8stesting.UpdateAction).GetObject().(*corev1.Node)
			if outOfService(n) != nil {
				l.ops = append(l.ops, "taint "+n.Name)
			} else {
				l.ops = append(l.ops, "untaint "+n.Name)
			}
		case a.GetVerb() == "delete" && a.GetResource().Resource == "pods":
			l.ops = append(l.ops, "delete-pod "+a.(k8stesting.DeleteAction).GetName())
		}
		return false, nil, nil
	})
	reg := prometheus.NewRegistry()
	l.metrics = NewMetrics(reg)
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := DefaultConfig()
	l.sup = &Supervisor{
		Collector: &Collector{Client: l.kube, Leases: l.leaseClient(), Fencer: l.power, Config: cfg, Clock: l.clk,
			Machines: map[string]string{"netci-lab-1": "netci-lab-1", "netci-lab-2": "netci-lab-2", "netci-lab-3": "netci-lab-3"},
			MaxGap:   3 * time.Second, StateTimeout: time.Second},
		Executor: &Executor{Client: l.kube, Leases: l.leaseClient(), Fencer: l.power, Clock: l.clk, Log: log,
			Events: l.events, Metrics: l.metrics, OffTimeout: 5 * time.Second},
		Config: cfg, Interval: time.Second, ActionTimeout: 10 * time.Second, AlertEvery: time.Minute,
		Clock: l.clk, Log: log, Metrics: l.metrics,
	}
	return l
}

func (l *lab) create(ns, name, holder string) {
	seconds, zero := int32(6), int32(0)
	if ns == "kube-node-lease" {
		seconds = 40
	}
	if _, err := l.leases.Leases(ns).Create(context.Background(), &coordinationv1.Lease{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec:       coordinationv1.LeaseSpec{HolderIdentity: &holder, LeaseDurationSeconds: &seconds, LeaseTransitions: &zero},
	}, metav1.CreateOptions{}); err != nil {
		l.t.Fatal(err)
	}
}

// leaseClient fails like the API server does while it is down, and logs lease writes.
func (l *lab) leaseClient() *recordingLeases { return &recordingLeases{lab: l} }

type recordingLeases struct{ lab *lab }

func (r *recordingLeases) Leases(ns string) coordinationclient.LeaseInterface {
	r.lab.mu.Lock()
	down := r.lab.apiDown
	r.lab.mu.Unlock()
	if down {
		return leasetest.Unreachable{}.Leases(ns)
	}
	return &recordingLease{LeaseInterface: r.lab.leases.Leases(ns), lab: r.lab}
}

// recordingLease logs the supervisor's lease writes; the holders in these tests write through
// lab.leases directly, so every write logged here is the supervisor's.
type recordingLease struct {
	coordinationclient.LeaseInterface
	lab *lab
}

func (r *recordingLease) Update(ctx context.Context, l *coordinationv1.Lease, o metav1.UpdateOptions) (*coordinationv1.Lease, error) {
	out, err := r.LeaseInterface.Update(ctx, l, o)
	if err == nil {
		r.lab.log("release-lease " + l.Name)
	}
	return out, err
}

func (l *lab) log(op string) {
	l.mu.Lock()
	l.ops = append(l.ops, op)
	l.mu.Unlock()
}

func (l *lab) renew(ns, name string) {
	ls := l.leases.Leases(ns)
	cur, err := ls.Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		l.t.Fatal(err)
	}
	now := metav1.NewMicroTime(l.clk.Now())
	cur.Spec.RenewTime = &now
	if _, err := ls.Update(context.Background(), cur, metav1.UpdateOptions{}); err != nil {
		l.t.Fatal(err)
	}
}

// second advances one second: whatever is alive renews, then the supervisor ticks.
func (l *lab) second() {
	l.clk.Step(time.Second)
	l.mu.Lock()
	down, alive, cell := l.apiDown, map[string]bool{}, l.cellAlive
	for k, v := range l.alive {
		alive[k] = v
	}
	l.mu.Unlock()
	if !down {
		for node, ok := range alive {
			if ok {
				l.renew("kube-node-lease", node)
			}
		}
		if cell {
			l.renew("cell-a", "jenkins")
		}
	}
	l.sup.Tick(context.Background())
}

func (l *lab) until(what string, max int, cond func() bool) int {
	l.t.Helper()
	for i := 1; i <= max; i++ {
		l.second()
		if cond() {
			return i
		}
	}
	l.t.Fatalf("%s did not happen within %d s; ops %v", what, max, l.opsCopy())
	return 0
}

func (l *lab) opsCopy() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]string(nil), l.ops...)
}

func (l *lab) has(op string) bool {
	for _, o := range l.opsCopy() {
		if o == op {
			return true
		}
	}
	return false
}

// inOrder fails unless every op appears, each after the previous one.
func (l *lab) inOrder(ops ...string) {
	l.t.Helper()
	got, i := l.opsCopy(), 0
	for _, o := range got {
		if i < len(ops) && o == ops[i] {
			i++
		}
	}
	if i != len(ops) {
		l.t.Fatalf("want in order %v, got %v", ops, got)
	}
}

func (l *lab) cellLease() *coordinationv1.Lease {
	return l.leases.Get("cell-a", "jenkins")
}

func (l *lab) node(name string) *corev1.Node {
	n, err := l.kube.CoreV1().Nodes().Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		l.t.Fatal(err)
	}
	return n
}

func (l *lab) warm() {
	for i := 0; i < 30; i++ {
		l.second()
	}
	if ops := l.opsCopy(); len(ops) != 0 {
		l.t.Fatalf("acted on a healthy cluster: %v", ops)
	}
}

func (l *lab) kill(node string) {
	l.mu.Lock()
	l.alive[node], l.cellAlive = false, false
	l.mu.Unlock()
}

type fakePower struct {
	mu     sync.Mutex
	states map[string]fence.State
	lab    *lab
	// onState runs on every State call, outside the lock.
	onState func(machine string)
}

func (p *fakePower) Name() string { return "fake" }
func (p *fakePower) State(_ context.Context, m string) (fence.State, error) {
	if p.onState != nil {
		p.onState(m)
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	s, ok := p.states[m]
	if !ok {
		return fence.Unknown, fmt.Errorf("no such machine %s", m)
	}
	if s == fence.Off {
		p.lab.log("confirmed-off " + m)
	}
	return s, nil
}
func (p *fakePower) PowerOff(_ context.Context, m string) error {
	p.mu.Lock()
	p.states[m] = fence.Off
	p.mu.Unlock()
	p.lab.log("power-off " + m)
	return nil
}
func (p *fakePower) PowerOn(_ context.Context, m string) error {
	p.mu.Lock()
	p.states[m] = fence.Running
	p.mu.Unlock()
	p.lab.log("power-on " + m)
	return nil
}
func (p *fakePower) set(m string, s fence.State) {
	p.mu.Lock()
	p.states[m] = s
	p.mu.Unlock()
}

type fakeEvents struct {
	mu      sync.Mutex
	reasons []string
}

func (f *fakeEvents) Event(_ ObjectRef, _ bool, reason, _ string) {
	f.mu.Lock()
	f.reasons = append(f.reasons, reason)
	f.mu.Unlock()
}
func (f *fakeEvents) has(reason string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, r := range f.reasons {
		if r == reason {
			return true
		}
	}
	return false
}

func TestAPowerLossIsFencedWithinTheLeaseDurationAndInTheSafeOrder(t *testing.T) {
	l := newLab(t)
	l.warm()
	l.kill("netci-lab-1")
	l.power.set("netci-lab-1", fence.Off)

	took := l.until("fencing", 30, func() bool { return l.has("delete-pod jenkins-0") })
	if took > 7 {
		t.Fatalf("fenced %d s after the power loss; the lease duration is 6 s", took)
	}
	if l.has("power-off netci-lab-1") {
		t.Fatal("powered off a machine that was already off")
	}
	l.inOrder("confirmed-off netci-lab-1", "release-lease jenkins", "taint netci-lab-1", "not-ready netci-lab-1", "delete-pod jenkins-0",
		"delete-pod longhorn-manager-a")
	if l.has("delete-pod longhorn-manager-b") {
		t.Fatal("deleted a pod on a machine that is running")
	}
	got := l.cellLease()
	if got.Spec.HolderIdentity != nil || got.Annotations[lease.FencedAnnotation] != holder {
		t.Fatalf("lease not released for the fenced holder: %+v", got)
	}
	if n := l.node("netci-lab-1"); outOfService(n) == nil || n.Annotations[FencedByAnnotation] == "" {
		t.Fatal("node not taken out of service")
	}
	if !l.events.has("Fenced") {
		t.Fatal("no event recorded")
	}
	for _, c := range l.node("netci-lab-1").Status.Conditions {
		if c.Type == corev1.NodeReady && (c.Status != corev1.ConditionUnknown || c.Reason != "NetciFenced") {
			t.Fatalf("the fenced node still reads %s", c.Status)
		}
	}

	// The StatefulSet's replacement acquires the lease: the supervisor reports the takeover.
	_ = l.kube.CoreV1().Pods("cell-a").Delete(context.Background(), "jenkins-0", metav1.DeleteOptions{})
	if _, err := l.kube.CoreV1().Pods("cell-a").Create(context.Background(), &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "jenkins-0", Namespace: "cell-a", UID: "uid-2", Labels: map[string]string{CellLabel: "true"}},
		Spec:       corev1.PodSpec{NodeName: "netci-lab-2"}}, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	cur := l.cellLease()
	next, one := "jenkins-0/uid-2", int32(1)
	cur.Spec.HolderIdentity, cur.Spec.LeaseTransitions = &next, &one
	if _, err := l.leases.Leases("cell-a").Update(context.Background(), cur, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	l.second()
	if !l.events.has("TakeoverComplete") || testutil.CollectAndCount(l.metrics.takeoverSeconds) != 1 {
		t.Fatal("takeover not reported")
	}
}

func TestAHungMachineIsPoweredOffOnlyOnceItsKubeletIsSilent(t *testing.T) {
	l := newLab(t)
	l.warm()
	l.kill("netci-lab-1") // the machine runs, but nothing on it is heard from

	took := l.until("power-off", 40, func() bool { return l.has("power-off netci-lab-1") })
	if took < int(l.sup.Config.NodeStale/time.Second) {
		t.Fatalf("powered off after %d s, before the kubelet was silent for %s", took, l.sup.Config.NodeStale)
	}
	l.until("fencing", 5, func() bool { return l.has("delete-pod jenkins-0") })
	l.inOrder("power-off netci-lab-1", "confirmed-off netci-lab-1", "release-lease jenkins", "taint netci-lab-1", "delete-pod jenkins-0")
	sts, _ := l.kube.AppsV1().StatefulSets("cell-a").Get(context.Background(), "jenkins", metav1.GetOptions{})
	if sts.Annotations[LastActionAnnotation] == "" {
		t.Fatal("the power-off was not recorded for the cooldown")
	}
}

func TestALeaseRenewedAfterItsMachineWasConfirmedOffStopsTheFencing(t *testing.T) {
	// The node-to-machine mapping is wrong: the machine said to be cell-a's is off, but the
	// controller is elsewhere and was only paused. It renews just as the supervisor acts.
	l := newLab(t)
	l.warm()
	l.mu.Lock()
	l.cellAlive = false
	l.mu.Unlock()
	l.power.set("netci-lab-1", fence.Off)
	var once sync.Once
	l.power.onState = func(string) {
		if len(l.opsCopy()) > 0 { // the executor's own check, after the decision
			once.Do(func() { l.renew("cell-a", "jenkins") })
		}
	}
	l.until("abort", 30, func() bool { return l.events.has("FenceAborted") })
	if l.has("taint netci-lab-1") || l.has("delete-pod jenkins-0") {
		t.Fatalf("went on after the lease moved: %v", l.opsCopy())
	}
	if got := l.cellLease(); got.Spec.HolderIdentity == nil || *got.Spec.HolderIdentity != holder {
		t.Fatal("the live holder lost its lease")
	}
	if testutil.ToFloat64(l.metrics.leaseMoved) != 1 {
		t.Fatal("not counted")
	}
}

func TestAfterAnAPIOutageNothingIsFencedOnWhatTheSupervisorDidNotSee(t *testing.T) {
	// Every holder and every kubelet is cut off with the supervisor. When the API returns, the
	// leases all look unchanged for longer than any threshold -- because nobody could write.
	l := newLab(t)
	// With every node silent, the mass-failure guard and the control-plane majority check would
	// each hold back on their own; take both out of play so this tests the reset alone.
	l.sup.Config.PanicFraction = 1
	n := l.node("netci-lab-1")
	delete(n.Labels, controlPlaneLabel)
	if _, err := l.kube.CoreV1().Nodes().Update(context.Background(), n, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	l.mu.Lock()
	l.ops = nil
	l.mu.Unlock()
	l.warm()
	l.mu.Lock()
	l.apiDown = true
	l.mu.Unlock()
	for i := 0; i < 40; i++ {
		l.second()
	}
	l.mu.Lock()
	l.apiDown = false
	l.mu.Unlock()
	l.sup.Tick(context.Background()) // the supervisor looks before anyone has renewed
	for i := 0; i < 30; i++ {
		l.second()
	}
	if ops := l.opsCopy(); len(ops) != 0 {
		t.Fatalf("acted on a stale picture: %v", ops)
	}
	if testutil.ToFloat64(l.metrics.resets) < 1 {
		t.Fatal("the gap did not reset the observations")
	}
}

func TestAFencedMachineIsBroughtBackOnlyWhenAsked(t *testing.T) {
	l := newLab(t)
	l.sup.Config.AutoPowerOn = true
	l.warm()
	l.kill("netci-lab-1")
	l.power.set("netci-lab-1", fence.Off)
	l.until("fencing", 10, func() bool { return l.has("delete-pod jenkins-0") })
	_ = l.kube.CoreV1().Pods("cell-a").Delete(context.Background(), "jenkins-0", metav1.DeleteOptions{})
	for i := 0; i < 60; i++ {
		l.second()
	}
	if l.has("power-on netci-lab-1") {
		t.Fatal("powered the machine on while its cell was not held by anyone yet")
	}
	// The StatefulSet's replacement starts elsewhere and takes the lease.
	if _, err := l.kube.CoreV1().Pods("cell-a").Create(context.Background(), &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "jenkins-0", Namespace: "cell-a", UID: "uid-2", Labels: map[string]string{CellLabel: "true"}},
		Spec:       corev1.PodSpec{NodeName: "netci-lab-2"}}, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	cur := l.cellLease()
	next, one := "jenkins-0/uid-2", int32(1)
	cur.Spec.HolderIdentity, cur.Spec.LeaseTransitions = &next, &one
	if _, err := l.leases.Leases("cell-a").Update(context.Background(), cur, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	l.mu.Lock()
	l.cellAlive = true // the new holder renews
	l.mu.Unlock()

	l.until("power-on", 60, func() bool { return l.has("power-on netci-lab-1") })
	l.mu.Lock()
	l.alive["netci-lab-1"] = true // it booted and its kubelet is back, and reports Ready
	l.mu.Unlock()
	n := l.node("netci-lab-1")
	n.Status.Conditions = []corev1.NodeCondition{{Type: corev1.NodeReady, Status: corev1.ConditionTrue}}
	if _, err := l.kube.CoreV1().Nodes().UpdateStatus(context.Background(), n, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	l.until("unfence", 5, func() bool { return l.has("untaint netci-lab-1") })
	if n := l.node("netci-lab-1"); outOfService(n) != nil || n.Annotations[FencedByAnnotation] != "" {
		t.Fatal("still out of service")
	}
}

func TestATaintTheSupervisorDidNotAddIsLeftAlone(t *testing.T) {
	l := newLab(t)
	n := l.node("netci-lab-2")
	n.Spec.Taints = append(n.Spec.Taints, corev1.Taint{Key: OutOfServiceTaint, Value: "nodeshutdown", Effect: corev1.TaintEffectNoExecute})
	if _, err := l.kube.CoreV1().Nodes().Update(context.Background(), n, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	l.mu.Lock()
	l.ops = nil
	l.mu.Unlock()
	l.warm()
	if outOfService(l.node("netci-lab-2")) == nil {
		t.Fatal("removed an operator's taint")
	}
}

func TestAStandbyObservesButDoesNotActAndActsAtOnceWhenItLeads(t *testing.T) {
	l := newLab(t)
	var leading bool
	l.sup.Leading = func() bool { return leading }
	l.warm()
	l.kill("netci-lab-1")
	l.power.set("netci-lab-1", fence.Off)
	for i := 0; i < 20; i++ {
		l.second()
	}
	if l.has("delete-pod jenkins-0") || l.has("taint netci-lab-1") {
		t.Fatalf("a standby acted: %v", l.opsCopy())
	}
	// It becomes the leader: its observations are already old enough to act on, at once.
	leading = true
	l.second()
	if !l.has("delete-pod jenkins-0") {
		t.Fatalf("the new leader waited to observe again: %v", l.opsCopy())
	}
}
