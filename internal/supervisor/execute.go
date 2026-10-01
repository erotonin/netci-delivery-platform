package supervisor

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	"k8s.io/client-go/util/retry"
	"k8s.io/utils/clock"

	"github.com/erotonin/netci-delivery-platform/internal/fence"
	"github.com/erotonin/netci-delivery-platform/internal/lease"
)

// ErrLeaseMoved: the cell's Lease changed after its machine was confirmed off. Whatever renewed
// it is not on that machine, so the node-to-machine mapping (or something else the supervisor
// believes) is wrong. Fencing stops there, before anything lets a successor start.
var ErrLeaseMoved = errors.New("the lease changed after its holder's machine was confirmed off")

// Executor carries out Decide's actions. Every step that lets a successor start comes after
// the power controller has confirmed the machine off.
type Executor struct {
	Client     kubernetes.Interface
	Leases     coordinationclient.LeasesGetter // see Collector.Leases
	Fencer     fence.Fencer
	Clock      clock.PassiveClock
	Log        *slog.Logger
	Events     EventSink
	Metrics    *Metrics
	OffTimeout time.Duration
}

// EventSink records a Kubernetes Event about an object; tests use a fake.
type EventSink interface {
	Event(obj ObjectRef, warning bool, reason, message string)
}

// ObjectRef names what an Event is about.
type ObjectRef struct {
	Kind, Namespace, Name string
	UID                   types.UID
}

// Execute runs one action. l is the cell's Lease as the decision saw it (FenceNode only).
func (e *Executor) Execute(ctx context.Context, a Action, l *coordinationv1.Lease) error {
	var err error
	switch a.Kind {
	case FenceNode:
		err = e.fenceNode(ctx, a, l)
	case DeletePod:
		err = e.deletePod(ctx, a, false)
	case ForceDeletePods:
		err = e.forceDeleteOnFencedNode(ctx, a)
	case Unfence:
		err = e.unfence(ctx, a)
	case PowerOn:
		err = e.Fencer.PowerOn(ctx, a.Machine)
		if err == nil {
			e.Events.Event(nodeRef(a.Node), false, "PoweredOn", a.Reason)
		}
	case Alert:
		e.Metrics.alerts.Inc()
		ref := ObjectRef{Kind: "StatefulSet", Namespace: a.Namespace, Name: a.Cell}
		if a.Cell == "" {
			ref = nodeRef(a.Node)
		}
		if ref.Name != "" {
			e.Events.Event(ref, true, "SupervisorAlert", a.Reason)
		}
		return nil
	}
	e.Metrics.actions.WithLabelValues(string(a.Kind), result(err)).Inc()
	return err
}

func (e *Executor) fenceNode(ctx context.Context, a Action, l *coordinationv1.Lease) error {
	log := e.Log.With("cell", a.Namespace+"/"+a.Cell, "node", a.Node, "machine", a.Machine, "holder", a.Holder)
	began := e.Clock.Now()
	cellRef := ObjectRef{Kind: "StatefulSet", Namespace: a.Namespace, Name: a.Cell}
	if l == nil {
		return errors.New("no lease snapshot for the cell being fenced")
	}

	// 1. The machine must be off, confirmed by its power controller.
	if a.PowerOff {
		if err := e.stampLastAction(ctx, a); err != nil {
			return err // the cooldown must survive a restart before anything is powered off
		}
		e.Events.Event(cellRef, true, "PoweringOff", fmt.Sprintf("%s: %s", a.Machine, a.Reason))
		if _, err := fence.EnsureOff(ctx, e.Fencer, a.Machine, e.OffTimeout); err != nil {
			e.Events.Event(cellRef, true, "FenceFailed", err.Error())
			return fmt.Errorf("power off: %w", err)
		}
	} else {
		state, err := e.Fencer.State(ctx, a.Machine)
		if err != nil || state != fence.Off {
			return fmt.Errorf("machine %s no longer reports off (%s, %v): deciding again", a.Machine, state, err)
		}
	}
	offAt := e.Clock.Now()
	log.Info("machine confirmed off", "powered_off", a.PowerOff, "took", offAt.Sub(began))

	// 2. Give the Lease up on the fenced holder's behalf, conditional on it being exactly as
	// observed. Nothing on a machine that is off can renew it, so a conflict means the holder
	// is somewhere else: stop.
	if _, err := lease.ReleaseFenced(ctx, e.Leases, l); err != nil {
		if !apierrors.IsConflict(err) {
			return fmt.Errorf("release lease: %w", err)
		}
		cur, gerr := e.Leases.Leases(l.Namespace).Get(ctx, l.Name, metav1.GetOptions{})
		if gerr != nil {
			return fmt.Errorf("release lease: %w (and re-reading it: %v)", err, gerr)
		}
		// Another supervisor replica got there first: same outcome.
		alreadyReleased := (cur.Spec.HolderIdentity == nil || *cur.Spec.HolderIdentity == "") &&
			cur.Annotations[lease.FencedAnnotation] == a.Holder
		if !alreadyReleased {
			e.Metrics.leaseMoved.Inc()
			msg := fmt.Sprintf("%v: %s was off, yet the lease (held by %q) was renewed; check the node-to-machine mapping. Not fencing node %s.",
				ErrLeaseMoved, a.Machine, a.Holder, a.Node)
			e.Events.Event(cellRef, true, "FenceAborted", msg)
			log.Error(msg)
			return ErrLeaseMoved
		}
	}

	// 3. Out of service: Kubernetes detaches the node's volumes without waiting for its kubelet.
	if err := e.taint(ctx, a.Node); err != nil {
		return fmt.Errorf("taint: %w", err)
	}
	// 3b. Say what was proven: the node is not ready. Storage such as Longhorn moves a volume
	// only once the node reads NotReady, which the node lifecycle controller writes 40-50 s after
	// the last heartbeat -- longer when its own leader died on the same machine. In the lab that
	// wait was most of a takeover (45 s of attach). The kubelet writes Ready again when the
	// machine comes back.
	if err := e.markNotReady(ctx, a.Node); err != nil {
		log.Warn("could not mark the node not ready; storage will wait for Kubernetes to", "error", err)
	}
	// 4. Delete the pod now rather than wait for the pod garbage collector's next pass. Only the
	// pod that held the Lease: the UID precondition never touches a replacement.
	if err := e.deletePod(ctx, a, true); err != nil {
		return err
	}
	done := e.Clock.Now()
	e.Metrics.fenceSeconds.Observe(done.Sub(began).Seconds())
	e.Events.Event(cellRef, true, "Fenced", fmt.Sprintf("machine %s confirmed off in %s; lease released, node %s out of service, pod deleted (%s in all)",
		a.Machine, offAt.Sub(began).Round(time.Millisecond), a.Node, done.Sub(began).Round(time.Millisecond)))
	log.Warn("cell fenced", "machine_off_after", offAt.Sub(began), "total", done.Sub(began), "epoch", lease.EpochOf(l))
	return nil
}

func (e *Executor) taint(ctx context.Context, node string) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		n, err := e.Client.CoreV1().Nodes().Get(ctx, node, metav1.GetOptions{})
		if err != nil {
			return err
		}
		if n.Annotations == nil {
			n.Annotations = map[string]string{}
		}
		n.Annotations[FencedByAnnotation] = "netci-supervisor"
		if outOfService(n) == nil {
			now := metav1.NewTime(e.Clock.Now())
			n.Spec.Taints = append(n.Spec.Taints, corev1.Taint{Key: OutOfServiceTaint, Value: "nodeshutdown",
				Effect: corev1.TaintEffectNoExecute, TimeAdded: &now})
		}
		_, err = e.Client.CoreV1().Nodes().Update(ctx, n, metav1.UpdateOptions{})
		return err
	})
}

func (e *Executor) markNotReady(ctx context.Context, node string) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		n, err := e.Client.CoreV1().Nodes().Get(ctx, node, metav1.GetOptions{})
		if err != nil {
			return err
		}
		now := metav1.NewTime(e.Clock.Now())
		found := false
		for i := range n.Status.Conditions {
			c := &n.Status.Conditions[i]
			if c.Type != corev1.NodeReady {
				continue
			}
			found = true
			if c.Status == corev1.ConditionTrue {
				c.LastTransitionTime = now
			}
			c.Status, c.Reason, c.LastHeartbeatTime = corev1.ConditionUnknown, "NetciFenced", now
			c.Message = "netci-supervisor confirmed the machine off with its power controller"
		}
		if !found {
			n.Status.Conditions = append(n.Status.Conditions, corev1.NodeCondition{Type: corev1.NodeReady,
				Status: corev1.ConditionUnknown, Reason: "NetciFenced", LastTransitionTime: now, LastHeartbeatTime: now,
				Message: "netci-supervisor confirmed the machine off with its power controller"})
		}
		_, err = e.Client.CoreV1().Nodes().UpdateStatus(ctx, n, metav1.UpdateOptions{})
		return err
	})
}

func (e *Executor) unfence(ctx context.Context, a Action) error {
	err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		n, err := e.Client.CoreV1().Nodes().Get(ctx, a.Node, metav1.GetOptions{})
		if err != nil {
			return err
		}
		if n.Annotations[FencedByAnnotation] == "" {
			return nil // not ours
		}
		kept := n.Spec.Taints[:0]
		for _, t := range n.Spec.Taints {
			if t.Key != OutOfServiceTaint {
				kept = append(kept, t)
			}
		}
		n.Spec.Taints = kept
		delete(n.Annotations, FencedByAnnotation)
		_, err = e.Client.CoreV1().Nodes().Update(ctx, n, metav1.UpdateOptions{})
		return err
	})
	if err == nil {
		e.Events.Event(nodeRef(a.Node), false, "Unfenced", a.Reason)
		e.Log.Info("node back in service", "node", a.Node)
	}
	return err
}

func (e *Executor) deletePod(ctx context.Context, a Action, force bool) error {
	if !force {
		if err := e.stampLastAction(ctx, a); err != nil {
			return err
		}
	}
	uid := types.UID(a.PodUID)
	opts := metav1.DeleteOptions{Preconditions: &metav1.Preconditions{UID: &uid}}
	if force {
		zero := int64(0)
		opts.GracePeriodSeconds = &zero
	}
	err := e.Client.CoreV1().Pods(a.Namespace).Delete(ctx, a.Pod, opts)
	if apierrors.IsNotFound(err) || apierrors.IsConflict(err) {
		return nil // already gone, or already replaced
	}
	if err != nil {
		return fmt.Errorf("delete pod %s/%s: %w", a.Namespace, a.Pod, err)
	}
	if !force {
		e.Events.Event(ObjectRef{Kind: "StatefulSet", Namespace: a.Namespace, Name: a.Cell}, true, "PodRestarted", a.Reason)
	}
	return nil
}

// forceDeleteOnFencedNode finishes a fencing that stopped between the taint and the deletion.
// It asks the power controller again first: force-deleting is only safe on a machine that is off.
func (e *Executor) forceDeleteOnFencedNode(ctx context.Context, a Action) error {
	state, err := e.Fencer.State(ctx, a.Machine)
	if err != nil || state != fence.Off {
		return fmt.Errorf("machine %s no longer reports off (%s, %v): not force-deleting", a.Machine, state, err)
	}
	pods, err := e.Client.CoreV1().Pods(metav1.NamespaceAll).List(ctx, metav1.ListOptions{
		LabelSelector: CellLabel, FieldSelector: "spec.nodeName=" + a.Node})
	if err != nil {
		return err
	}
	for _, p := range pods.Items {
		pa := Action{Namespace: p.Namespace, Pod: p.Name, PodUID: string(p.UID)}
		if err := e.deletePod(ctx, pa, true); err != nil {
			return err
		}
		e.Log.Warn("force-deleted a cell pod left on a fenced node", "pod", p.Namespace+"/"+p.Name, "node", a.Node)
	}
	return nil
}

func (e *Executor) stampLastAction(ctx context.Context, a Action) error {
	patch := fmt.Sprintf(`{"metadata":{"annotations":{%q:%q}}}`, LastActionAnnotation, e.Clock.Now().UTC().Format(time.RFC3339))
	_, err := e.Client.AppsV1().StatefulSets(a.Namespace).Patch(ctx, a.Cell, types.MergePatchType, []byte(patch), metav1.PatchOptions{})
	if err != nil {
		return fmt.Errorf("record the action on %s/%s: %w", a.Namespace, a.Cell, err)
	}
	return nil
}

func nodeRef(name string) ObjectRef { return ObjectRef{Kind: "Node", Name: name, UID: types.UID(name)} }

func result(err error) string {
	if err != nil {
		return "error"
	}
	return "ok"
}
