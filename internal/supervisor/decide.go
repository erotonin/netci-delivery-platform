// Package supervisor detects a cell whose controller can no longer hold its Lease, fences the
// machine it ran on, and lets Kubernetes start the controller elsewhere (ADR-060).
//
// Decide is the whole policy, as a pure function of what was observed: it performs no I/O, so
// every rule that keeps two controllers from writing one JENKINS_HOME is tested directly. The
// collector builds its input; the executor carries out what it returns.
//
// What makes a takeover safe is not this package but the order the executor follows: a machine
// is confirmed off by its power controller before anything that lets a successor start. This
// package decides *whether* to start that sequence, and stays out of the way when it cannot
// tell what is going on.
package supervisor

import (
	"fmt"
	"sort"
	"time"

	"github.com/erotonin/netci-delivery-platform/internal/fence"
)

// Config holds the policy's thresholds.
type Config struct {
	// NodeStale: a node whose kubelet heartbeat has not changed for this long, on the
	// supervisor's clock, is treated as unable to manage its pods. Kubelets renew every 10 s.
	NodeStale time.Duration
	// StuckPodAfter: when a cell's Lease has not changed for this long while its node's
	// kubelet is alive, the pod is deleted so that it restarts. Shorter waits would race the
	// agent restarting by itself.
	StuckPodAfter time.Duration
	// Cooldown: after the supervisor powered a cell's machine off or deleted its pod, it does
	// neither again for that cell within this time; a second failure so soon is more likely a
	// fault in what the supervisor sees than in the cell.
	Cooldown time.Duration
	// PanicFraction: when the cells of at least two machines (or two nodes' kubelets), and more
	// than this fraction of them, fail at once, nothing running is powered off and no pod is deleted -- a shared cause
	// (the network, the API server) is more likely, and acting would make it worse.
	PanicFraction float64
	// AutoPowerOn starts a fenced machine again once nothing of a cell is left on its node.
	// Off in production (an operator decides when a failed machine returns), on in the lab so
	// failure runs can repeat.
	AutoPowerOn bool
	// PowerOnAfter: minimum time a machine stays fenced before an automatic power-on.
	PowerOnAfter time.Duration
	// SuspectAfter: a Lease its pod has not renewed for this long (several renewal intervals)
	// gets its machine's power state asked at once. A machine that reports off is fenced then,
	// without waiting for the Lease to expire: nothing runs on a machine that is off, and a holder
	// alive elsewhere (a wrong mapping) would have renewed meanwhile. Power losses -- the commonest
	// failure -- are taken over seconds sooner; a hung or cut-off machine still waits for expiry.
	SuspectAfter time.Duration
	// StorageSettle: how long after the taint the cells' pods on a fenced node are deleted. Their
	// deletion starts the volumes' detach, and one asked for while the storage is still taking in
	// that the node is gone can wait for the dead machine: in the lab, every pod deleted within
	// 0.3 s of its node being marked not ready (5 of 5) cost its takeover about 10 s more, while
	// Longhorn tried to reach the engine on the machine that was off before it gave up and the
	// detach was retried (lab/spike/many_volumes_probe.py, ADR-070). The Leases are released at
	// fencing, so nothing else waits meanwhile. Zero: at the next observation.
	StorageSettle time.Duration
}

// DefaultConfig is the production default.
func DefaultConfig() Config {
	return Config{NodeStale: 20 * time.Second, StuckPodAfter: 30 * time.Second, Cooldown: 2 * time.Minute,
		PanicFraction: 0.5, PowerOnAfter: 30 * time.Second, SuspectAfter: 3 * time.Second,
		StorageSettle: 3 * time.Second}
}

// CellView is what was observed of one cell.
type CellView struct {
	Namespace string
	Name      string // the StatefulSet
	Pod       string // its controller pod, "<name>-0"

	LeaseHolder string
	LeaseEpoch  int32
	// LeaseExpired: the supervisor has seen the Lease unchanged for its whole duration, in an
	// unbroken run of observations.
	LeaseExpired   bool
	LeaseUnchanged time.Duration
	// LeaseQuiet: how long the Lease has had the same resourceVersion, across gaps in the
	// observations too -- reads are quorum reads, so a version seen again was not renewed in
	// between. It only prompts asking the machine's power state; nothing is fenced on it unless
	// the power controller says the machine is off. LeaseUnchanged, reset by a gap, still
	// decides expiry and everything done on a guess. (After a power loss the observations failed
	// for a few seconds while etcd elected a leader, and the reset added 4 s to fencing.)
	LeaseQuiet time.Duration

	PodExists   bool
	PodUID      string
	PodNode     string
	PodDeleting bool
	LastAction  time.Time // the supervisor last powered off or deleted for this cell; zero if never
	// AwaitingTakeover: the supervisor released this cell's Lease for a fenced holder and no pod
	// holds it again yet -- its volume may still be on its way to the new node.
	AwaitingTakeover bool
	// UnschedulableFor: how long the scheduler has found no node for the cell's pod; zero when
	// it is placed or still being considered.
	UnschedulableFor time.Duration
	// VolumeFailed: the pod holding the Lease reports that its JENKINS_HOME no longer takes writes
	// (the report's detail); empty otherwise.
	VolumeFailed string
}

// suspect: the Lease names this pod and has gone unrenewed for SuspectAfter, so its machine's
// power is worth asking about now.
func (c CellView) suspect(cfg Config) bool {
	return c.holdsLease() && (c.LeaseExpired || (cfg.SuspectAfter > 0 && max(c.LeaseUnchanged, c.LeaseQuiet) >= cfg.SuspectAfter))
}

// holdsLease: the Lease names this exact pod. A Lease left by an earlier pod of the same name
// is not this pod's to lose; its successor takes it over by itself once it has expired.
func (c CellView) holdsLease() bool {
	return c.PodExists && c.LeaseHolder != "" && c.LeaseHolder == c.Pod+"/"+c.PodUID
}

// NodeView is what was observed of one node.
type NodeView struct {
	Name         string
	Machine      string // the power controller's name for it; "" if not configured
	Ready        bool
	ControlPlane bool
	// KubeletFresh: its heartbeat changed within NodeStale, or the supervisor has not yet
	// watched it that long (an unknown heartbeat counts as alive, never as dead).
	KubeletFresh bool
	Fenced       bool          // carries the out-of-service taint
	FencedFor    time.Duration // since the taint was added
	MachineState fence.State   // fence.Unknown when not queried
	CellPods     int           // cell controller pods still bound to the node
	Attachments  int           // VolumeAttachments still naming the node
}

// Input is one observation of the whole cluster.
type Input struct {
	Now   time.Time
	Cells []CellView
	Nodes map[string]NodeView
}

// Kind of action.
type Kind string

const (
	// FenceNode: power the machine off if PowerOff, confirm it is off, release the cell's Lease
	// on behalf of the fenced holder, add the out-of-service taint, delete the node's other pods.
	// The cells' pods are left to ForceDeletePods (Config.StorageSettle).
	FenceNode Kind = "FenceNode"
	// DeletePod: the node's kubelet is alive; delete the controller pod gracefully.
	DeletePod Kind = "DeletePod"
	// ForceDeletePods: the node is fenced and its machine off, and cell pods (Action.Pods) are
	// still bound to it: those whose Lease was released for them or has expired.
	ForceDeletePods Kind = "ForceDeletePods"
	// Unfence: the machine is running again and its node Ready; remove the taint.
	Unfence Kind = "Unfence"
	// PowerOn: start a fenced machine again (AutoPowerOn only).
	PowerOn Kind = "PowerOn"
	// Alert: something needs a person; nothing was done.
	Alert Kind = "Alert"
)

// Action is one thing to do and why.
type Action struct {
	Kind      Kind
	Namespace string
	Cell      string
	Pod       string
	PodUID    string
	Node      string
	Machine   string
	Holder    string   // the identity being fenced
	PowerOff  bool     // FenceNode: the machine is running and must be stopped first
	Pods      []PodRef // ForceDeletePods: the pods to delete, each only if it is still the pod observed
	Reason    string
}

// PodRef names one pod as observed: a deletion carries its UID, so that it never touches a
// replacement of the same name.
type PodRef struct{ Namespace, Name, UID string }

// Decide returns what to do about the observed cluster. At most one machine that is running is
// powered off per decision; the next decision sees its effect.
func Decide(in Input, cfg Config) []Action {
	var actions []Action

	var lost []CellView // cells whose own pod stopped renewing
	for _, c := range in.Cells {
		switch {
		case c.holdsLease() && c.LeaseExpired:
			lost = append(lost, c)
		case c.suspect(cfg) && in.Nodes[c.PodNode].MachineState == fence.Off && !in.Nodes[c.PodNode].Fenced:
			// Not expired yet, but its machine is off: that is the answer the expiry waits for.
			lost = append(lost, c)
		}
	}
	sort.Slice(lost, func(i, j int) bool {
		return lost[i].Namespace+"/"+lost[i].Name < lost[j].Namespace+"/"+lost[j].Name
	})

	panicking, why := panicked(in, lost, cfg)
	if panicking {
		actions = append(actions, Action{Kind: Alert, Reason: why})
	}

	actions = append(actions, failedVolumes(in, cfg)...)

	powerOffDecided := false
	for _, c := range lost {
		a, ok := decideCell(c, in, cfg)
		if !ok {
			continue
		}
		if a.Kind == DeletePod || (a.Kind == FenceNode && a.PowerOff) {
			if panicking {
				continue // the alert above covers it
			}
			if a.Kind == FenceNode {
				if powerOffDecided {
					continue
				}
				powerOffDecided = true
			}
		}
		actions = append(actions, a)
	}
	return append(actions, recovery(in, cfg)...)
}

// failedVolumes restarts the pods whose controller reports that its JENKINS_HOME fails: a new pod
// mounts the volume afresh. Many at once is a storage problem, which restarting every controller
// would not fix: then a person is told instead.
func failedVolumes(in Input, cfg Config) []Action {
	var failing []CellView
	for _, c := range in.Cells {
		if c.VolumeFailed != "" && c.holdsLease() && !c.LeaseExpired && !c.PodDeleting {
			failing = append(failing, c)
		}
	}
	if len(failing) >= 2 && float64(len(failing)) > cfg.PanicFraction*float64(len(in.Cells)) {
		return []Action{{Kind: Alert, Reason: fmt.Sprintf("%d of %d cells report that their JENKINS_HOME fails: a storage problem; not restarting them",
			len(failing), len(in.Cells))}}
	}
	var actions []Action
	for _, c := range failing {
		a := Action{Namespace: c.Namespace, Cell: c.Name, Pod: c.Pod, PodUID: c.PodUID, Node: c.PodNode, Holder: c.LeaseHolder}
		if !c.LastAction.IsZero() && in.Now.Sub(c.LastAction) < cfg.Cooldown {
			a.Kind, a.Reason = Alert, cooldownReason(c, in.Now, cfg)+" (its JENKINS_HOME fails: "+c.VolumeFailed+")"
		} else {
			a.Kind, a.Reason = DeletePod, "the controller reports that its JENKINS_HOME fails ("+c.VolumeFailed+"): restarting the pod so that the volume is mounted again"
		}
		actions = append(actions, a)
	}
	return actions
}

// panicked reports whether failures look shared rather than independent.
func panicked(in Input, lost []CellView, cfg Config) (bool, string) {
	// Counted by machine, not by cell: cells that stopped renewing together on one machine are
	// one failure, which that machine explains. Counted by cell, two cells sharing a machine that
	// hung looked like "2 of 2 cells at once", and the supervisor left both controllers down
	// waiting for a person (lab, chaos series 18).
	unhandled := map[string]bool{}
	for _, c := range lost {
		if n, ok := in.Nodes[c.PodNode]; ok && n.Fenced {
			continue // already being dealt with
		}
		unhandled[c.PodNode] = true
	}
	hosting := map[string]bool{}
	for _, c := range in.Cells {
		if c.PodNode != "" {
			hosting[c.PodNode] = true
		}
	}
	if len(unhandled) >= 2 && float64(len(unhandled)) > cfg.PanicFraction*float64(len(hosting)) {
		return true, fmt.Sprintf("cells on %d of the %d machines that run cells stopped renewing at once: powering nothing off and deleting no pod (a shared cause is more likely)",
			len(unhandled), len(hosting))
	}
	var stale, total int
	for _, n := range in.Nodes {
		total++
		if !n.KubeletFresh && !n.Fenced {
			stale++
		}
	}
	if stale >= 2 && float64(stale) > cfg.PanicFraction*float64(total) {
		return true, fmt.Sprintf("%d of %d nodes stopped heartbeating at once: powering nothing off and deleting no pod (a shared cause is more likely)",
			stale, total)
	}
	return false, ""
}

func decideCell(c CellView, in Input, cfg Config) (Action, bool) {
	a := Action{Namespace: c.Namespace, Cell: c.Name, Pod: c.Pod, PodUID: c.PodUID, Node: c.PodNode, Holder: c.LeaseHolder}
	if c.PodNode == "" {
		return Action{}, false // never scheduled: nothing ran, nothing to fence
	}
	n, ok := in.Nodes[c.PodNode]
	if !ok {
		a.Kind, a.Reason = Alert, fmt.Sprintf("the controller stopped renewing and its node %s is not known", c.PodNode)
		return a, true
	}
	a.Machine = n.Machine
	if n.Fenced {
		return Action{}, false // recover() finishes what fencing started
	}
	if n.Machine == "" {
		a.Kind, a.Reason = Alert, fmt.Sprintf("the controller stopped renewing and node %s has no power controller configured", n.Name)
		return a, true
	}
	coolingDown := !c.LastAction.IsZero() && in.Now.Sub(c.LastAction) < cfg.Cooldown

	switch n.MachineState {
	case fence.Off:
		// Safe whatever else is going on: nothing runs on a machine that is off.
		a.Kind, a.Reason = FenceNode, "the controller stopped renewing and its machine is off"
		return a, true

	case fence.Running:
		if n.KubeletFresh {
			if c.PodDeleting || c.LeaseUnchanged < cfg.StuckPodAfter {
				return Action{}, false // the kubelet is alive; give the agent time to recover
			}
			if coolingDown {
				a.Kind, a.Reason = Alert, cooldownReason(c, in.Now, cfg)
				return a, true
			}
			a.Kind = DeletePod
			a.Reason = fmt.Sprintf("the controller has not renewed for %s while its node's kubelet is alive: restarting the pod",
				c.LeaseUnchanged.Round(time.Second))
			return a, true
		}
		if coolingDown {
			a.Kind, a.Reason = Alert, cooldownReason(c, in.Now, cfg)
			return a, true
		}
		if n.ControlPlane && !quorumWithout(in.Nodes, n.Name) {
			a.Kind = Alert
			a.Reason = fmt.Sprintf("node %s is unresponsive, but powering it off would leave the control plane without a majority", n.Name)
			return a, true
		}
		a.Kind, a.PowerOff = FenceNode, true
		a.Reason = "the controller stopped renewing and its node stopped heartbeating: powering the machine off"
		return a, true

	default:
		a.Kind = Alert
		a.Reason = fmt.Sprintf("the controller stopped renewing and the power state of %s is unknown: not fencing on a guess", n.Machine)
		return a, true
	}
}

func cooldownReason(c CellView, now time.Time, cfg Config) string {
	return fmt.Sprintf("the controller stopped renewing again %s after the last action on this cell: waiting out the %s cooldown",
		now.Sub(c.LastAction).Round(time.Second), cfg.Cooldown)
}

// recovery finishes fencing that was interrupted, and brings fenced machines back.
//
// A fenced machine is powered on only once every cell is held again. In the lab, a machine
// brought back 30 s after its fencing came up while its cell's volume was still moving; the
// storage then deleted the replacement pod it had almost started, and the takeover took a
// second round.
//
// A cell whose pod no node can take is not moving a volume, though, and the fenced machine may
// be the only room left for it: in the lab a cell waited for a node while the supervisor waited
// for the cell, with its machine off, until a person stepped in. Such a cell does not hold the
// machines off, and it is reported: the cluster has no headroom for a takeover.
func recovery(in Input, cfg Config) []Action {
	settled := true
	var actions []Action
	for _, c := range in.Cells {
		switch {
		case !c.AwaitingTakeover:
		case c.UnschedulableFor >= cfg.PowerOnAfter:
			actions = append(actions, Action{Kind: Alert, Namespace: c.Namespace, Cell: c.Name, Reason: fmt.Sprintf(
				"no node has had room for the cell's controller for %s since its takeover: the cluster lacks the headroom a takeover needs",
				c.UnschedulableFor.Round(time.Second))})
		default:
			settled = false
		}
	}
	names := make([]string, 0, len(in.Nodes))
	for name := range in.Nodes {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		n := in.Nodes[name]
		if !n.Fenced {
			continue
		}
		a := Action{Node: n.Name, Machine: n.Machine}
		switch {
		case n.MachineState == fence.Off && n.CellPods > 0:
			if n.FencedFor < cfg.StorageSettle {
				continue // the storage is still taking in that the node is gone
			}
			deletable, alerts := cellPodsToDelete(in, n)
			actions = append(actions, alerts...)
			if len(deletable) == 0 {
				continue
			}
			a.Kind, a.Pods, a.Reason = ForceDeletePods, deletable, "the node is fenced and its machine off, but cell pods are still bound to it"
		case n.MachineState == fence.Running && n.Ready && n.KubeletFresh:
			a.Kind, a.Reason = Unfence, "the machine is running again and its node is Ready"
		case cfg.AutoPowerOn && settled && n.MachineState == fence.Off && n.CellPods == 0 && n.Attachments == 0 && n.FencedFor >= cfg.PowerOnAfter:
			a.Kind, a.Reason = PowerOn, "nothing of a cell is left on the fenced node: powering it back on"
		default:
			continue
		}
		actions = append(actions, a)
	}
	return actions
}

// cellPodsToDelete: the cell pods bound to a fenced node whose Lease no longer names them -- the
// supervisor released it when fencing -- or names them but has expired. A pod that still renews
// is running somewhere, whatever the power controller says of this node's machine (a wrong
// mapping): it is never deleted, and a person is told.
func cellPodsToDelete(in Input, n NodeView) ([]PodRef, []Action) {
	var pods []PodRef
	var alerts []Action
	for _, c := range in.Cells {
		if !c.PodExists || c.PodNode != n.Name {
			continue
		}
		if c.holdsLease() && !c.LeaseExpired {
			alerts = append(alerts, Action{Kind: Alert, Namespace: c.Namespace, Cell: c.Name, Node: n.Name, Reason: fmt.Sprintf(
				"node %s is fenced and machine %s reports off, yet %s/%s still renews its Lease: not deleting it; check the node-to-machine mapping",
				n.Name, n.Machine, c.Namespace, c.Pod)})
			continue
		}
		pods = append(pods, PodRef{Namespace: c.Namespace, Name: c.Pod, UID: c.PodUID})
	}
	return pods, alerts
}

// quorumWithout reports whether, without skip, the control-plane nodes still heartbeating are a
// majority of all control-plane nodes.
func quorumWithout(nodes map[string]NodeView, skip string) bool {
	total, alive := 0, 0
	for _, n := range nodes {
		if !n.ControlPlane {
			continue
		}
		total++
		if n.Name != skip && n.KubeletFresh && !n.Fenced {
			alive++
		}
	}
	return alive > total/2
}
