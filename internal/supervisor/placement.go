package supervisor

import corev1 "k8s.io/api/core/v1"

// YieldTo says whether the leader self should hand its leadership to another replica, and to
// which: when self shares a machine with a cell's controller and another Ready replica runs on a
// machine with none. A leader lost with a cell's machine adds its own lease expiry to that cell's
// takeover: in the lab, 20.3 s to fencing where a leader elsewhere fenced in 2.7 s. With every
// machine hosting a cell nothing is gained by moving, and nothing is done.
func YieldTo(self string, supervisors, cells []corev1.Pod) (string, bool) {
	busy := map[string]bool{}
	for _, c := range cells {
		if c.Spec.NodeName != "" && c.DeletionTimestamp == nil && c.Status.Phase != corev1.PodSucceeded && c.Status.Phase != corev1.PodFailed {
			busy[c.Spec.NodeName] = true
		}
	}
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
		for _, c := range s.Status.Conditions {
			if c.Type == corev1.PodReady && c.Status == corev1.ConditionTrue {
				return s.Name, true
			}
		}
	}
	return "", false
}
