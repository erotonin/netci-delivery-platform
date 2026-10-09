package fabric

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
	"k8s.io/utils/clock"
)

// Reconciler makes the pods match the rows, and keeps each pool's warm sandboxes topped up.
type Reconciler struct {
	Store    *Store
	Client   kubernetes.Interface
	Pods     PodSettings
	Pools    map[string]Pool
	Bindings *Bindings
	Clock    clock.PassiveClock
	Log      *slog.Logger
	Metrics  *Metrics
	// BindTimeout: a claim whose sandbox has not taken its binding by then is given up.
	BindTimeout time.Duration
	// StartTimeout: a sandbox not warm by then (image pull stuck, unschedulable) is failed.
	StartTimeout time.Duration
}

// Tick is one pass.
func (r *Reconciler) Tick(ctx context.Context) error {
	pods, err := r.Client.CoreV1().Pods(r.Pods.Namespace).List(ctx, metav1.ListOptions{LabelSelector: labelSandbox})
	if err != nil {
		return err
	}
	rows, err := r.Store.Live(ctx)
	if err != nil {
		return err
	}
	byName := map[string]*corev1.Pod{}
	for i := range pods.Items {
		byName[pods.Items[i].Name] = &pods.Items[i]
	}
	live := map[string]bool{}
	count := map[string]map[State]int{}
	now := r.Clock.Now()
	for _, sb := range rows {
		live[sb.Pod] = true
		if count[sb.Pool] == nil {
			count[sb.Pool] = map[State]int{}
		}
		count[sb.Pool][sb.State]++
		r.one(ctx, sb, byName[sb.Pod], now)
	}
	// A pod with no live row: its row was deleted, or it was never ours to keep.
	for name, p := range byName {
		if !live[name] && p.DeletionTimestamp == nil {
			r.Log.Warn("deleting a sandbox pod with no live sandbox", "pod", name)
			r.deletePod(ctx, name)
		}
	}
	for name, p := range r.Pools {
		c := count[name]
		warmish := c[Creating] + c[Warm]
		total := c[Creating] + c[Warm] + c[Claimed] + c[Bound]
		r.Metrics.setPool(name, c)
		for warmish < p.Warm && total < p.Max {
			sb, err := r.Store.Create(ctx, name)
			if err != nil {
				return err
			}
			r.createPod(ctx, sb)
			warmish++
			total++
		}
	}
	return nil
}

func (r *Reconciler) one(ctx context.Context, sb *Sandbox, pod *corev1.Pod, now time.Time) {
	terminated := pod != nil && (pod.Status.Phase == corev1.PodFailed || pod.Status.Phase == corev1.PodSucceeded)
	switch sb.State {
	case Creating:
		switch {
		case pod == nil:
			r.createPod(ctx, sb)
		case terminated:
			r.move(ctx, sb, Failed, "", "the pod ended before it was warm")
		case ready(pod):
			r.move(ctx, sb, Warm, string(pod.UID), "")
		case now.Sub(sb.CreatedAt) > r.StartTimeout:
			r.move(ctx, sb, Failed, "", "not warm within the start timeout")
		}
	case Warm:
		if pod == nil || terminated {
			r.move(ctx, sb, Failed, "", "the warm pod disappeared or ended")
		}
	case Claimed:
		switch {
		case pod == nil && sb.Cold && sb.PodUID == "":
			r.createPod(ctx, sb) // claimed before it existed
		case pod == nil || terminated:
			r.move(ctx, sb, Failed, "", "the claimed pod disappeared or ended")
		case sb.ClaimedAt != nil && now.Sub(*sb.ClaimedAt) > r.BindTimeout:
			r.Bindings.Drop(sb.ID)
			r.move(ctx, sb, Released, "", "not bound within the bind timeout")
		default:
			if sb.PodUID == "" {
				_ = r.Store.SetPodUID(ctx, sb.ID, string(pod.UID))
			}
			r.protect(ctx, pod)
		}
	case Bound:
		if pod == nil || terminated {
			r.move(ctx, sb, Failed, "", "the pod running a build disappeared or ended")
		} else {
			r.protect(ctx, pod)
		}
	case Released, Failed:
		r.Bindings.Drop(sb.ID)
		if pod == nil {
			r.move(ctx, sb, Deleted, "", "")
			return
		}
		if pod.DeletionTimestamp == nil {
			r.deletePod(ctx, sb.Pod)
		}
	}
}

func (r *Reconciler) move(ctx context.Context, sb *Sandbox, to State, podUID, reason string) {
	err := r.Store.Transition(ctx, sb.ID, sb.State, to, podUID, reason)
	switch {
	case err == nil:
		r.Metrics.transitions.WithLabelValues(sb.Pool, string(to)).Inc()
		if to == Failed {
			r.Log.Warn("sandbox failed", "sandbox", sb.ID, "pod", sb.Pod, "pool", sb.Pool, "reason", reason)
		}
		if to == Released || to == Failed {
			r.Bindings.Drop(sb.ID)
			r.deletePod(ctx, sb.Pod) // now, not on the next pass
		}
	case errors.Is(err, ErrConflict):
	default:
		r.Log.Error("sandbox transition", "sandbox", sb.ID, "to", to, "error", err)
	}
}

func (r *Reconciler) createPod(ctx context.Context, sb *Sandbox) {
	p, ok := r.Pools[sb.Pool]
	if !ok {
		r.move(ctx, sb, Failed, "", "its pool is no longer configured")
		return
	}
	_, err := r.Client.CoreV1().Pods(r.Pods.Namespace).Create(ctx, r.Pods.pod(sb, p), metav1.CreateOptions{})
	if err != nil && !apierrors.IsAlreadyExists(err) {
		r.Log.Error("create sandbox pod", "sandbox", sb.ID, "error", err)
		if apierrors.IsInvalid(err) || apierrors.IsForbidden(err) {
			r.move(ctx, sb, Failed, "", "the pod was refused: "+err.Error())
		}
	}
}

// protect marks a sandbox that was warm when its pod was created, and is now claimed, as not to
// be evicted by a node autoscaler.
func (r *Reconciler) protect(ctx context.Context, pod *corev1.Pod) {
	if pod.Annotations[safeToEvict] == "false" && pod.Labels[LabelBusy] == "true" {
		return
	}
	patch, _ := json.Marshal(map[string]any{"metadata": map[string]any{"annotations": evictable(true),
		"labels": map[string]string{LabelBusy: "true"}}})
	_, err := r.Client.CoreV1().Pods(r.Pods.Namespace).Patch(ctx, pod.Name, types.MergePatchType, patch, metav1.PatchOptions{})
	if err != nil && !apierrors.IsNotFound(err) {
		r.Log.Error("protect a claimed sandbox from eviction", "pod", pod.Name, "error", err)
	}
}

func (r *Reconciler) deletePod(ctx context.Context, name string) {
	zero := int64(0)
	err := r.Client.CoreV1().Pods(r.Pods.Namespace).Delete(ctx, name, metav1.DeleteOptions{GracePeriodSeconds: &zero})
	if err != nil && !apierrors.IsNotFound(err) {
		r.Log.Error("delete sandbox pod", "pod", name, "error", err)
	}
}

func ready(p *corev1.Pod) bool {
	if p.Status.Phase != corev1.PodRunning {
		return false
	}
	for _, c := range p.Status.Conditions {
		if c.Type == corev1.PodReady {
			return c.Status == corev1.ConditionTrue
		}
	}
	return false
}
