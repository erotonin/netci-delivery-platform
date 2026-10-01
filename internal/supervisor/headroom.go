package supervisor

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/pager"
	"k8s.io/klog/v2"
)

// Room is whether a cell's controller could be placed elsewhere if its machine were lost.
type Room struct {
	Cell string // "<namespace>/<statefulset>"
	// Fits is false only when no other machine could take the controller even if every pod
	// of lower priority gave way: then a takeover of this cell cannot succeed. True is a
	// necessary condition, not a promise -- the lost machine's other pods also need room, and
	// constraints of other pods are not modelled.
	Fits   bool
	Best   string // the eligible machine with the most memory left, if any
	Reason string
}

// Headroom checks every cell's controller pod against the other machines. It is what the lab
// lacked: a takeover that fenced correctly and then had nowhere to run the controller, found
// only when a machine was lost.
func Headroom(nodes []corev1.Node, pods []corev1.Pod, cells []corev1.Pod) []Room {
	usedBy := map[string][]*corev1.Pod{}
	for i := range pods {
		p := &pods[i]
		if p.Spec.NodeName != "" && p.Status.Phase != corev1.PodSucceeded && p.Status.Phase != corev1.PodFailed {
			usedBy[p.Spec.NodeName] = append(usedBy[p.Spec.NodeName], p)
		}
	}
	var out []Room
	for i := range cells {
		c := &cells[i]
		need := requests(c)
		room := Room{Cell: c.Namespace + "/" + cellOf(c)}
		bestFree := int64(-1)
		var why []string
		for j := range nodes {
			n := &nodes[j]
			if n.Name == c.Spec.NodeName {
				continue
			}
			switch {
			case n.Spec.Unschedulable:
				why = append(why, n.Name+": cordoned")
				continue
			case !ready(n):
				why = append(why, n.Name+": not Ready")
				continue
			case !matches(c.Spec.NodeSelector, n.Labels):
				why = append(why, n.Name+": not selected")
				continue
			}
			if t, ok := untolerated(n.Spec.Taints, c.Spec.Tolerations); ok {
				why = append(why, n.Name+": taint "+t.Key)
				continue
			}
			// Pods of lower priority can be evicted for the controller; the others stay.
			cpu, mem := n.Status.Allocatable.Cpu().MilliValue(), n.Status.Allocatable.Memory().Value()
			for _, p := range usedBy[n.Name] {
				if p.UID == c.UID || priority(p) < priority(c) {
					continue
				}
				r := requests(p)
				cpu, mem = cpu-r.Cpu().MilliValue(), mem-r.Memory().Value()
			}
			if mem > bestFree {
				bestFree, room.Best = mem, n.Name
			}
			if cpu >= need.Cpu().MilliValue() && mem >= need.Memory().Value() {
				room.Fits = true
				continue
			}
			why = append(why, fmt.Sprintf("%s: %dm CPU and %s memory left for %dm and %s", n.Name, cpu,
				resource.NewQuantity(mem, resource.BinarySI), need.Cpu().MilliValue(), need.Memory()))
		}
		if !room.Fits {
			sort.Strings(why)
			room.Reason = fmt.Sprintf("no other machine could take the controller if %s were lost: %v", orNone(c.Spec.NodeName), why)
		}
		out = append(out, room)
	}
	return out
}

// requests is what the scheduler reserves for a pod: its containers and sidecars, or its
// largest ordinary init container with the sidecars, whichever is more, and the pod overhead.
func requests(p *corev1.Pod) corev1.ResourceList {
	sum := corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("0"), corev1.ResourceMemory: resource.MustParse("0")}
	sidecars := sum.DeepCopy()
	for _, ic := range p.Spec.InitContainers {
		if ic.RestartPolicy != nil && *ic.RestartPolicy == corev1.ContainerRestartPolicyAlways {
			add(sidecars, ic.Resources.Requests)
		}
	}
	for _, c := range p.Spec.Containers {
		add(sum, c.Resources.Requests)
	}
	add(sum, sidecars)
	for _, ic := range p.Spec.InitContainers {
		if ic.RestartPolicy == nil || *ic.RestartPolicy != corev1.ContainerRestartPolicyAlways {
			init := sidecars.DeepCopy()
			add(init, ic.Resources.Requests)
			for _, k := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
				if q := init[k]; q.Cmp(sum[k]) > 0 {
					sum[k] = q
				}
			}
		}
	}
	add(sum, p.Spec.Overhead)
	return sum
}

func add(to, from corev1.ResourceList) {
	for _, k := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
		if q, ok := from[k]; ok {
			v := to[k]
			v.Add(q)
			to[k] = v
		}
	}
}

func priority(p *corev1.Pod) int32 {
	if p.Spec.Priority != nil {
		return *p.Spec.Priority
	}
	return 0
}

func matches(selector, labels map[string]string) bool {
	for k, v := range selector {
		if labels[k] != v {
			return false
		}
	}
	return true
}

// untolerated returns a NoSchedule or NoExecute taint that none of the tolerations covers.
func untolerated(taints []corev1.Taint, tolerations []corev1.Toleration) (corev1.Taint, bool) {
	for _, t := range taints {
		if t.Effect == corev1.TaintEffectPreferNoSchedule {
			continue
		}
		covered := false
		for i := range tolerations {
			// Comparison operators counted as enabled: matching more keeps the check optimistic,
			// which a necessary condition must be.
			if tolerations[i].ToleratesTaint(klog.Background(), &t, true) {
				covered = true
				break
			}
		}
		if !covered {
			return t, true
		}
	}
	return corev1.Taint{}, false
}

func cellOf(p *corev1.Pod) string {
	for _, o := range p.OwnerReferences {
		if o.Kind == "StatefulSet" {
			return o.Name
		}
	}
	return p.Name
}

func orNone(node string) string {
	if node == "" {
		return "its machine"
	}
	return node
}

// HeadroomWatch checks the cells' headroom every Interval, apart from the supervisor's
// one-second observations: it lists every pod in the cluster, which may take longer than
// those are allowed. Every replica keeps the metric; the leader reports a cell that loses its
// headroom.
type HeadroomWatch struct {
	Client   kubernetes.Interface
	Interval time.Duration
	Leading  func() bool
	Events   EventSink
	Metrics  *Metrics
	Log      *slog.Logger
	short    map[string]bool
}

// Run checks until ctx is done.
func (w *HeadroomWatch) Run(ctx context.Context) {
	t := time.NewTicker(w.Interval)
	defer t.Stop()
	for {
		if err := w.check(ctx); err != nil && ctx.Err() == nil {
			w.Log.Warn("headroom not checked", "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

func (w *HeadroomWatch) check(ctx context.Context) error {
	sets, err := w.Client.AppsV1().StatefulSets(metav1.NamespaceAll).List(ctx, metav1.ListOptions{LabelSelector: CellLabel})
	if err != nil {
		return err
	}
	nodes, err := w.Client.CoreV1().Nodes().List(ctx, metav1.ListOptions{})
	if err != nil {
		return err
	}
	// Every pod in the cluster, a page at a time, keeping only what the check reads: a large
	// cluster's full pod list in one answer would weigh on the API server and on this process
	// every 30 s.
	cellPods := map[string]bool{}
	for _, s := range sets.Items {
		cellPods[s.Namespace+"/"+s.Name+"-0"] = true
	}
	var pods, cells []corev1.Pod
	err = pager.New(func(ctx context.Context, opts metav1.ListOptions) (runtime.Object, error) {
		return w.Client.CoreV1().Pods(metav1.NamespaceAll).List(ctx, opts)
	}).EachListItem(ctx, metav1.ListOptions{Limit: 500}, func(obj runtime.Object) error {
		p := slim(obj.(*corev1.Pod))
		pods = append(pods, p)
		if cellPods[p.Namespace+"/"+p.Name] {
			cells = append(cells, p)
		}
		return nil
	})
	if err != nil {
		return err
	}
	w.Metrics.SetSharing(Sharing(cells))
	if w.short == nil {
		w.short = map[string]bool{}
	}
	for _, r := range Headroom(nodes.Items, pods, cells) {
		w.Metrics.SetHeadroom(r.Cell, r.Fits)
		was := w.short[r.Cell]
		w.short[r.Cell] = !r.Fits
		if r.Fits || was || w.Leading == nil || !w.Leading() {
			continue
		}
		ns, name, _ := strings.Cut(r.Cell, "/")
		w.Log.Warn("a cell has no headroom for a takeover", "cell", r.Cell, "reason", r.Reason)
		w.Events.Event(ObjectRef{Kind: "StatefulSet", Namespace: ns, Name: name}, true, "NoTakeoverHeadroom", r.Reason)
	}
	return nil
}

// Sharing counts the cells whose controller shares its machine with another cell's: the cells
// one machine's loss would take together. Cells spread by a preferred rule, not a required one
// (a takeover onto the last machine with room must not be refused), so takeovers can leave
// them together; the lab found both of its cells on one machine after a series of power-offs.
// Moving one back is a restart of its controller, which is an operator's call, not the
// supervisor's.
func Sharing(cells []corev1.Pod) int {
	per := map[string]int{}
	for _, c := range cells {
		if c.Spec.NodeName != "" && c.Status.Phase != corev1.PodSucceeded && c.Status.Phase != corev1.PodFailed {
			per[c.Spec.NodeName]++
		}
	}
	n := 0
	for _, k := range per {
		if k > 1 {
			n += k
		}
	}
	return n
}

// slim keeps of a pod what Headroom and Sharing read.
func slim(p *corev1.Pod) corev1.Pod {
	out := corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: p.Name, Namespace: p.Namespace, UID: p.UID, OwnerReferences: p.OwnerReferences},
		Spec: corev1.PodSpec{NodeName: p.Spec.NodeName, Priority: p.Spec.Priority, NodeSelector: p.Spec.NodeSelector,
			Tolerations: p.Spec.Tolerations, Overhead: p.Spec.Overhead},
		Status: corev1.PodStatus{Phase: p.Status.Phase},
	}
	for _, c := range p.Spec.Containers {
		out.Spec.Containers = append(out.Spec.Containers, corev1.Container{Resources: corev1.ResourceRequirements{Requests: c.Resources.Requests}})
	}
	for _, c := range p.Spec.InitContainers {
		out.Spec.InitContainers = append(out.Spec.InitContainers, corev1.Container{RestartPolicy: c.RestartPolicy,
			Resources: corev1.ResourceRequirements{Requests: c.Resources.Requests}})
	}
	return out
}
