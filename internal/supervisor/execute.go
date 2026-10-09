package supervisor

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
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
	// QuietCheck: how long another cell's Lease must stay unchanged, on a machine being fenced,
	// before it is released with it -- more than one renewal interval. Zero: those Leases are
	// left to expire.
	QuietCheck time.Duration
	sleep      func(context.Context, time.Duration) bool // tests replace it
}

func (e *Executor) wait(ctx context.Context, d time.Duration) bool {
	if e.sleep != nil {
		return e.sleep(ctx, d)
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
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
	// 4. The cell's pod is not deleted here: ForceDeletePods does, Config.StorageSettle after the
	// taint, once the storage has taken in that the node is gone.
	// 5. Every other cell whose controller is on this machine is as dead as this one: give its
	// Lease up too, before its pod goes. Otherwise its replacement finds the Lease held by a pod
	// that no longer exists and waits out the Lease's duration (seen in the lab: 16 s more for the
	// second of two cells on one machine, fenced through the first).
	if !e.releaseCellsOn(ctx, a, log) {
		// Another cell's holder renewed while its pod is bound to this node: something on it may
		// be running after all. The fenced cell is done; the node's other pods are left alone.
		return fmt.Errorf("%w: another cell on node %s renewed its lease; not deleting the node's other pods", ErrLeaseMoved, a.Node)
	}
	// 6. Every pod on the machine but the cells': Kubernetes force-deletes them too once the node
	// is out of service, but only on the pod garbage collector's next pass (every 20 s). The
	// storage waits for that: Longhorn moves a volume only once the dead node's own
	// longhorn-manager pod is gone (in the lab, 30 s of a takeover). The machine is off; nothing
	// on it is running.
	if n, err := e.deletePodsOn(ctx, a.Node); err != nil {
		log.Warn("could not delete the other pods of the fenced node; the pod garbage collector will", "error", err)
	} else if n > 0 {
		log.Info("deleted the pods left on the fenced node", "pods", n)
	}
	done := e.Clock.Now()
	e.Metrics.fenceSeconds.Observe(done.Sub(began).Seconds())
	e.Events.Event(cellRef, true, "Fenced", fmt.Sprintf("machine %s confirmed off in %s; lease released, node %s out of service (%s in all); the cells' pods are deleted once the storage has settled",
		a.Machine, offAt.Sub(began).Round(time.Millisecond), a.Node, done.Sub(began).Round(time.Millisecond)))
	log.Warn("cell fenced", "machine_off_after", offAt.Sub(began), "total", done.Sub(began), "epoch", lease.EpochOf(l))
	return nil
}

// releaseCellsOn releases the Lease of every other cell whose controller pod is bound to the
// fenced node. Only after the node's machine is confirmed off, and on the same evidence as the
// cell being fenced: each Lease is read, then read again after QuietCheck (more than one renewal
// interval), and released only if it did not change in between -- conditional on that version.
// A Lease that changed has a live holder somewhere else, which the machine's state contradicts:
// that cell is left alone and the fault reported.
// It returns false when such a contradiction was found.
func (e *Executor) releaseCellsOn(ctx context.Context, a Action, log *slog.Logger) bool {
	pods, err := e.Client.CoreV1().Pods(metav1.NamespaceAll).List(ctx, metav1.ListOptions{
		FieldSelector: "spec.nodeName=" + a.Node, LabelSelector: CellLabel})
	if err != nil {
		log.Warn("could not list the other cells on the fenced node; their Leases will expire instead", "error", err)
		return true
	}
	type held struct {
		ref      ObjectRef
		identity string
		lease    *coordinationv1.Lease
	}
	var others []held
	for _, p := range pods.Items {
		if p.Spec.NodeName != a.Node || (p.Namespace == a.Namespace && p.Name == a.Pod) {
			continue
		}
		cell := statefulSetOf(&p)
		leaseName := cell
		if s, err := e.Client.AppsV1().StatefulSets(p.Namespace).Get(ctx, cell, metav1.GetOptions{}); err == nil && s.Annotations[LeaseAnnotation] != "" {
			leaseName = s.Annotations[LeaseAnnotation]
		}
		l, err := e.Leases.Leases(p.Namespace).Get(ctx, leaseName, metav1.GetOptions{})
		identity := p.Name + "/" + string(p.UID)
		if err != nil || l.Spec.HolderIdentity == nil || *l.Spec.HolderIdentity != identity {
			continue // not this pod's to give up
		}
		others = append(others, held{ObjectRef{Kind: "StatefulSet", Namespace: p.Namespace, Name: cell}, identity, l})
	}
	if len(others) == 0 || e.QuietCheck <= 0 || !e.wait(ctx, e.QuietCheck) {
		return true
	}
	consistent := true
	for _, o := range others {
		cur, err := e.Leases.Leases(o.lease.Namespace).Get(ctx, o.lease.Name, metav1.GetOptions{})
		if err == nil && cur.ResourceVersion == o.lease.ResourceVersion {
			_, err = lease.ReleaseFenced(ctx, e.Leases, cur)
		} else if err == nil {
			err = ErrLeaseMoved
		}
		if err != nil {
			if errors.Is(err, ErrLeaseMoved) || apierrors.IsConflict(err) {
				consistent = false
				e.Metrics.leaseMoved.Inc()
				e.Events.Event(o.ref, true, "FenceAborted", fmt.Sprintf("%v: %s was off, yet the lease (held by %q) was renewed; check the node-to-machine mapping",
					ErrLeaseMoved, a.Machine, o.identity))
			}
			log.Warn("did not release the lease of another cell on the fenced node", "cell", o.ref.Namespace+"/"+o.ref.Name, "error", err)
			continue
		}
		e.Events.Event(o.ref, true, "Fenced", fmt.Sprintf("machine %s confirmed off while fencing %s/%s; lease released", a.Machine, a.Namespace, a.Cell))
		log.Warn("lease released for another cell on the fenced node", "cell", o.ref.Namespace+"/"+o.ref.Name, "holder", o.identity)
	}
	return consistent
}

// statefulSetOf names the StatefulSet that owns a cell's controller pod: its owner, or its name
// without the ordinal.
func statefulSetOf(p *corev1.Pod) string {
	for _, o := range p.OwnerReferences {
		if o.Kind == "StatefulSet" {
			return o.Name
		}
	}
	if i := strings.LastIndex(p.Name, "-"); i > 0 {
		return p.Name[:i]
	}
	return p.Name
}

// deletePodsOn force-deletes every pod bound to node. Only after its machine is confirmed off.
func (e *Executor) deletePodsOn(ctx context.Context, node string) (int, error) {
	pods, err := e.Client.CoreV1().Pods(metav1.NamespaceAll).List(ctx, metav1.ListOptions{FieldSelector: "spec.nodeName=" + node})
	if err != nil {
		return 0, err
	}
	zero := int64(0)
	n := 0
	for _, p := range pods.Items {
		// The field selector already says so; checked again, because deleting a pod on a machine
		// that runs is the one thing this must never do.
		if p.Spec.NodeName != node {
			continue
		}
		if _, cell := p.Labels[CellLabel]; cell {
			continue // ForceDeletePods, after Config.StorageSettle
		}
		uid := p.UID
		err := e.Client.CoreV1().Pods(p.Namespace).Delete(ctx, p.Name, metav1.DeleteOptions{
			GracePeriodSeconds: &zero, Preconditions: &metav1.Preconditions{UID: &uid}})
		if err == nil {
			n++
		} else if !apierrors.IsNotFound(err) && !apierrors.IsConflict(err) {
			return n, err
		}
	}
	return n, nil
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

// forceDeleteOnFencedNode deletes the cells' pods on a fenced node that Decide named: those whose
// Lease was released for them or has expired. It asks the power controller again first:
// force-deleting is only safe on a machine that is off. Each deletion carries the UID observed,
// so a replacement of the same name is never touched.
func (e *Executor) forceDeleteOnFencedNode(ctx context.Context, a Action) error {
	state, err := e.Fencer.State(ctx, a.Machine)
	if err != nil || state != fence.Off {
		return fmt.Errorf("machine %s no longer reports off (%s, %v): not force-deleting", a.Machine, state, err)
	}
	for _, p := range a.Pods {
		if err := e.deletePod(ctx, Action{Namespace: p.Namespace, Pod: p.Name, PodUID: p.UID}, true); err != nil {
			return err
		}
		e.Log.Warn("force-deleted a cell pod on a fenced node", "pod", p.Namespace+"/"+p.Name, "node", a.Node)
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
