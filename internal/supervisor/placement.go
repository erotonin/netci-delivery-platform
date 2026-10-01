package supervisor

import corev1 "k8s.io/api/core/v1"

func readyPod(p corev1.Pod) bool {
	for _, c := range p.Status.Conditions {
		if c.Type == corev1.PodReady && c.Status == corev1.ConditionTrue {
			return true
		}
	}
	return false
}

func cellNodes(cells []corev1.Pod) map[string]bool {
	busy := map[string]bool{}
	for _, c := range cells {
		if c.Spec.NodeName != "" && c.DeletionTimestamp == nil && c.Status.Phase != corev1.PodSucceeded && c.Status.Phase != corev1.PodFailed {
			busy[c.Spec.NodeName] = true
		}
	}
	return busy
}

// YieldTo says whether the leader self should hand its leadership to another replica, and to
// which: when self shares a machine with a cell's controller and another Ready replica runs on a
// machine with none. A leader lost with a cell's machine adds its own lease expiry to that cell's
// takeover: in the lab, 20.3 s to fencing where a leader elsewhere fenced in 2.7 s. With every
// machine hosting a cell nothing is gained by moving, and nothing is done.
func YieldTo(self string, supervisors, cells []corev1.Pod) (string, bool) {
	busy := cellNodes(cells)
	mine := ""
	for _, s := range supervisors {
		if s.Name == self {
			mine = s.Spec.NodeName
		}
	}
	if mine == "" || !busy[mine] {
		return "", false
	}
	for _, s := range supervisors {
		if s.Name == self || s.Spec.NodeName == "" || s.Spec.NodeName == mine || busy[s.Spec.NodeName] || s.DeletionTimestamp != nil {
			continue
		}
		if readyPod(s) {
			return s.Name, true
		}
	}
	return "", false
}

// Rebalance names the standby replica to delete so that it is recreated on a machine without a
// cell, when the leader self is beside a cell, no replica is on such a machine, and one is free
// and ready to take it. Placement drifts with every takeover -- cells move, replicas do not -- and
// in the lab both replicas ended beside cells with a machine free: the next loss of the leader's
// machine was fenced at 19 s. The replica's own anti-affinity puts the new pod on the free
// machine; YieldTo then hands the leadership over.
func Rebalance(self string, supervisors, cells []corev1.Pod, nodes []corev1.Node) (corev1.Pod, bool) {
	if _, ok := YieldTo(self, supervisors, cells); ok {
		return corev1.Pod{}, false
	}
	busy := cellNodes(cells)
	used := map[string]bool{}
	mine := ""
	for _, s := range supervisors {
		used[s.Spec.NodeName] = true
		if s.Name == self {
			mine = s.Spec.NodeName
		}
	}
	if mine == "" || !busy[mine] {
		return corev1.Pod{}, false
	}
	free := false
	for i := range nodes {
		n := &nodes[i]
		if ready(n) && !n.Spec.Unschedulable && outOfService(n) == nil && !busy[n.Name] && !used[n.Name] {
			free = true
		}
	}
	if !free {
		return corev1.Pod{}, false
	}
	for _, s := range supervisors {
		if s.Name != self && s.DeletionTimestamp == nil && busy[s.Spec.NodeName] && readyPod(s) {
			return s, true
		}
	}
	return corev1.Pod{}, false
}
