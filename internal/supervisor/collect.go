package supervisor

import (
	"context"
	"fmt"
	"sync"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	"k8s.io/utils/clock"

	"github.com/erotonin/netci-delivery-platform/internal/fence"
	"github.com/erotonin/netci-delivery-platform/internal/lease"
)

const (
	// CellLabel marks a cell's StatefulSet and its controller pod.
	CellLabel = "netci.io/cell"
	// LeaseAnnotation on the StatefulSet names the cell's Lease; the StatefulSet's name if absent.
	LeaseAnnotation = "netci.io/lease"
	// LastActionAnnotation on the StatefulSet: when the supervisor last powered off or deleted
	// for this cell (RFC 3339). It survives a supervisor restart, so the cooldown does too.
	LastActionAnnotation = "netci.io/supervisor-last-action"
	// FencedByAnnotation on a Node marks an out-of-service taint as this supervisor's; a taint
	// someone else added is never removed by it.
	FencedByAnnotation = "netci.io/fenced-by"
	// OutOfServiceTaint is Kubernetes' own: pods are force-deleted from the node and their
	// volumes detached without waiting for the kubelet. It is only correct on a machine that is
	// off, which is why it is added after the power controller has said so.
	OutOfServiceTaint = "node.kubernetes.io/out-of-service"
	controlPlaneLabel = "node-role.kubernetes.io/control-plane"
	nodeLeaseNS       = "kube-node-lease"
)

// Collector builds Decide's input from the API server and the power controller.
//
// It reads directly every time instead of through informers: an informer's cache keeps serving
// the last state when the supervisor is cut off, and a Lease that looks unchanged because the
// supervisor stopped seeing changes would be fenced. For the same reason, a gap in successful
// observations longer than MaxGap makes it forget what it saw: after a gap, nothing is called
// expired or stale until it has been watched again for the full duration.
type Collector struct {
	Client kubernetes.Interface
	// Leases is Client.CoordinationV1() in production; tests give it one that keeps
	// resourceVersions, which client-go's fake does not.
	Leases   coordinationclient.LeasesGetter
	Fencer   fence.Fencer
	Machines map[string]string // node name -> machine name
	Config   Config
	Clock    clock.PassiveClock
	MaxGap   time.Duration
	// StateTimeout bounds each power-controller query.
	StateTimeout time.Duration

	cells  *lease.Observer
	beats  *lease.Observer
	lastOK time.Time
}

// Snapshot is one observation: Decide's input, and the Leases as read, for the executor's
// conditional updates.
type Snapshot struct {
	Input  Input
	Leases map[string]*coordinationv1.Lease // "<namespace>/<cell>"
	// Reset is true when this observation started over after a gap.
	Reset bool
}

// Collect observes the cluster once.
func (c *Collector) Collect(ctx context.Context) (*Snapshot, error) {
	start := c.Clock.Now()
	snap := &Snapshot{Leases: map[string]*coordinationv1.Lease{}}
	if c.cells == nil || c.lastOK.IsZero() || start.Sub(c.lastOK) > c.MaxGap {
		c.cells, c.beats = lease.NewObserver(c.Clock), lease.NewObserver(c.Clock)
		snap.Reset = true
	}

	sets, err := c.Client.AppsV1().StatefulSets(metav1.NamespaceAll).List(ctx, metav1.ListOptions{LabelSelector: CellLabel})
	if err != nil {
		return nil, fmt.Errorf("list cells: %w", err)
	}
	nodes, err := c.Client.CoreV1().Nodes().List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, fmt.Errorf("list nodes: %w", err)
	}
	beats, err := c.Leases.Leases(nodeLeaseNS).List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, fmt.Errorf("list node heartbeats: %w", err)
	}
	attachments, err := c.Client.StorageV1().VolumeAttachments().List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, fmt.Errorf("list volume attachments: %w", err)
	}

	in := Input{Now: start, Nodes: map[string]NodeView{}}
	for _, s := range sets.Items {
		cv := CellView{Namespace: s.Namespace, Name: s.Name, Pod: s.Name + "-0"}
		if t, err := time.Parse(time.RFC3339, s.Annotations[LastActionAnnotation]); err == nil {
			cv.LastAction = t
		}
		leaseName := s.Name
		if n := s.Annotations[LeaseAnnotation]; n != "" {
			leaseName = n
		}
		l, err := c.Leases.Leases(s.Namespace).Get(ctx, leaseName, metav1.GetOptions{})
		switch {
		case apierrors.IsNotFound(err):
			c.cells.Forget(s.Namespace, leaseName) // never held yet: nothing to lose
		case err != nil:
			return nil, fmt.Errorf("lease of %s/%s: %w", s.Namespace, s.Name, err)
		default:
			obs := c.cells.Observe(l)
			cv.LeaseHolder, cv.LeaseEpoch = obs.Holder, obs.Epoch
			cv.LeaseExpired = obs.Holder != "" && obs.Expired
			cv.LeaseUnchanged = obs.SinceChange
			snap.Leases[s.Namespace+"/"+s.Name] = l
		}
		pod, err := c.Client.CoreV1().Pods(s.Namespace).Get(ctx, cv.Pod, metav1.GetOptions{})
		switch {
		case apierrors.IsNotFound(err):
		case err != nil:
			return nil, fmt.Errorf("pod of %s/%s: %w", s.Namespace, s.Name, err)
		default:
			cv.PodExists, cv.PodUID, cv.PodNode = true, string(pod.UID), pod.Spec.NodeName
			cv.PodDeleting = pod.DeletionTimestamp != nil
		}
		in.Cells = append(in.Cells, cv)
	}

	heartbeat := map[string]*coordinationv1.Lease{}
	for i := range beats.Items {
		heartbeat[beats.Items[i].Name] = &beats.Items[i]
	}
	for i := range nodes.Items {
		n := &nodes.Items[i]
		nv := NodeView{Name: n.Name, Machine: c.Machines[n.Name], Ready: ready(n), KubeletFresh: true}
		_, nv.ControlPlane = n.Labels[controlPlaneLabel]
		if hb := heartbeat[n.Name]; hb != nil {
			nv.KubeletFresh = c.beats.Observe(hb).SinceChange < c.Config.NodeStale
		}
		if t := outOfService(n); t != nil && n.Annotations[FencedByAnnotation] != "" {
			nv.Fenced = true
			if t.TimeAdded != nil {
				nv.FencedFor = start.Sub(t.TimeAdded.Time)
			}
		}
		nv.MachineState = fence.Unknown
		in.Nodes[n.Name] = nv
	}
	for _, va := range attachments.Items {
		if nv, ok := in.Nodes[va.Spec.NodeName]; ok {
			nv.Attachments++
			in.Nodes[va.Spec.NodeName] = nv
		}
	}
	for _, cv := range in.Cells {
		if nv, ok := in.Nodes[cv.PodNode]; ok && cv.PodExists {
			nv.CellPods++
			in.Nodes[cv.PodNode] = nv
		}
	}

	// The power controller is asked only about machines a decision may turn on: those of cells
	// that stopped renewing, and fenced ones. Polling every machine every second would load it
	// for nothing.
	ask := map[string]bool{}
	for _, cv := range in.Cells {
		if cv.holdsLease() && cv.LeaseExpired {
			ask[cv.PodNode] = true
		}
	}
	for name, nv := range in.Nodes {
		if nv.Fenced {
			ask[name] = true
		}
	}
	c.queryMachines(ctx, in.Nodes, ask)

	in.Now = c.Clock.Now()
	c.lastOK = in.Now
	snap.Input = in
	return snap, nil
}

func (c *Collector) queryMachines(ctx context.Context, nodes map[string]NodeView, ask map[string]bool) {
	var mu sync.Mutex
	var wg sync.WaitGroup
	for name := range ask {
		nv, ok := nodes[name]
		if !ok || nv.Machine == "" {
			continue
		}
		wg.Add(1)
		go func(name, machine string) {
			defer wg.Done()
			qctx, cancel := context.WithTimeout(ctx, c.StateTimeout)
			defer cancel()
			state, err := c.Fencer.State(qctx, machine)
			if err != nil {
				state = fence.Unknown // never guessed as off
			}
			mu.Lock()
			nv := nodes[name]
			nv.MachineState = state
			nodes[name] = nv
			mu.Unlock()
		}(name, nv.Machine)
	}
	wg.Wait()
}

func ready(n *corev1.Node) bool {
	for _, c := range n.Status.Conditions {
		if c.Type == corev1.NodeReady {
			return c.Status == corev1.ConditionTrue
		}
	}
	return false
}

func outOfService(n *corev1.Node) *corev1.Taint {
	for i := range n.Spec.Taints {
		if n.Spec.Taints[i].Key == OutOfServiceTaint {
			return &n.Spec.Taints[i]
		}
	}
	return nil
}
