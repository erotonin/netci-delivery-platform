package fabric

import (
	"fmt"
	"regexp"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Pool is a kind of sandbox: what a build gets, and how many are kept warm.
type Pool struct {
	Name   string   `json:"name"`
	Labels []string `json:"labels"` // Jenkins labels this pool serves
	// Image holds the agent: Java and agent.jar at AgentJar (jenkins/inbound-agent does), plus
	// the build's toolchain.
	Image    string `json:"image"`
	AgentJar string `json:"agentJar,omitempty"`
	Warm     int    `json:"warm"`
	Max      int    `json:"max"`
	CPU      string `json:"cpu"`
	Memory   string `json:"memory"`
	Disk     string `json:"disk"` // ephemeral storage, workspace included
	// RuntimeClass: "" for the node's default (runc); kata or gvisor for stronger isolation.
	RuntimeClass string `json:"runtimeClass,omitempty"`
	// UserNamespace: the sandbox's root is not root on the host (hostUsers: false).
	UserNamespace bool              `json:"userNamespace"`
	NodeSelector  map[string]string `json:"nodeSelector,omitempty"`
}

var poolName = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$`)

func (p Pool) validate() error {
	switch {
	case !poolName.MatchString(p.Name):
		return fmt.Errorf("pool name %q must be a short DNS label", p.Name)
	case len(p.Labels) == 0:
		return fmt.Errorf("pool %s serves no label", p.Name)
	case p.Image == "":
		return fmt.Errorf("pool %s has no image", p.Name)
	case p.Warm < 0 || p.Max <= 0 || p.Warm > p.Max:
		return fmt.Errorf("pool %s: need 0 <= warm <= max and max > 0", p.Name)
	}
	for _, q := range []string{p.CPU, p.Memory, p.Disk} {
		if _, err := resource.ParseQuantity(q); err != nil {
			return fmt.Errorf("pool %s: resource %q: %w", p.Name, q, err)
		}
	}
	return nil
}

// PodSpec settings shared by every pool.
type PodSettings struct {
	Namespace      string
	ServiceAccount string // has no permissions; its token proves the pod's identity to the fabric
	BootstrapImage string // the netCI image, which carries netci-sandbox
	FabricURL      string
	Audience       string
	PullSecrets    []string // image pull secrets in the sandbox namespace
	// PriorityClass of sandbox pods. Below the controllers': when a machine is lost and its
	// controller needs room, the scheduler evicts sandboxes for it rather than leave the
	// controller -- and every build it carries -- waiting (seen in the lab). Empty: none.
	PriorityClass string
}

const (
	labelSandbox = "netci.io/sandbox"
	labelPool    = "netci.io/pool"
	// LabelBusy marks a sandbox running a build. A PodDisruptionBudget on it (the netci chart)
	// steers the scheduler's preemption: warm and claimed sandboxes share one priority, and a
	// controller that needs room after a takeover otherwise took a running build's sandbox as
	// readily as an idle one.
	LabelBusy = "netci.io/busy"

	// What node autoscalers read before evicting a pod to remove its node: cluster-autoscaler's
	// and Karpenter's. A sandbox has no controller of its own, which both would otherwise treat
	// as blocking the node for good.
	safeToEvict  = "cluster-autoscaler.kubernetes.io/safe-to-evict"
	doNotDisrupt = "karpenter.sh/do-not-disrupt"
)

// evictable is a sandbox's stance towards node autoscalers: a warm one is spare and may go
// with its node; one claimed for a build may not, or the build dies with a scale-down.
func evictable(claimed bool) map[string]string {
	if claimed {
		return map[string]string{safeToEvict: "false", doNotDisrupt: "true"}
	}
	return map[string]string{safeToEvict: "true"}
}

func sandboxLabels(sb *Sandbox, p Pool) map[string]string {
	l := map[string]string{labelSandbox: sb.ID.String(), labelPool: p.Name}
	if sb.State == Claimed || sb.State == Bound {
		l[LabelBusy] = "true"
	}
	return l
}

// pod builds a sandbox's pod. The pod has no Kubernetes API credential, only a token whose
// audience is the fabric; the build runs as a non-root user with no capabilities, within
// limits, in a workspace that disappears with it.
func (ps PodSettings) pod(sb *Sandbox, p Pool) *corev1.Pod {
	no, yes := false, true
	uid := int64(1000)
	expiry := int64(600)
	limits := corev1.ResourceList{
		corev1.ResourceCPU:              resource.MustParse(p.CPU),
		corev1.ResourceMemory:           resource.MustParse(p.Memory),
		corev1.ResourceEphemeralStorage: resource.MustParse(p.Disk),
	}
	restricted := &corev1.SecurityContext{
		AllowPrivilegeEscalation: &no,
		Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		RunAsNonRoot:             &yes,
		RunAsUser:                &uid,
	}
	jar := p.AgentJar
	if jar == "" {
		jar = "/usr/share/jenkins/agent.jar"
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: sb.Pod, Namespace: ps.Namespace,
			Labels:      sandboxLabels(sb, p),
			Annotations: evictable(sb.State == Claimed || sb.State == Bound),
		},
		Spec: corev1.PodSpec{
			RestartPolicy:                 corev1.RestartPolicyNever,
			ServiceAccountName:            ps.ServiceAccount,
			AutomountServiceAccountToken:  &no,
			EnableServiceLinks:            &no,
			TerminationGracePeriodSeconds: ptr(int64(10)),
			ImagePullSecrets:              pullSecrets(ps.PullSecrets),
			PriorityClassName:             ps.PriorityClass,
			NodeSelector:                  p.NodeSelector,
			SecurityContext: &corev1.PodSecurityContext{
				RunAsNonRoot: &yes, RunAsUser: &uid, RunAsGroup: &uid, FSGroup: &uid,
				SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
			},
			InitContainers: []corev1.Container{{
				Name: "install", Image: ps.BootstrapImage,
				Command:         []string{"/netci-sandbox", "install", "/netci-bin"},
				SecurityContext: restricted,
				Resources:       corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("64Mi")}},
				VolumeMounts:    []corev1.VolumeMount{{Name: "netci-bin", MountPath: "/netci-bin"}},
			}},
			Containers: []corev1.Container{{
				Name: "agent", Image: p.Image,
				Command: []string{"/netci-bin/netci-sandbox"},
				Env: []corev1.EnvVar{
					{Name: "NETCI_FABRIC_URL", Value: ps.FabricURL},
					{Name: "NETCI_FABRIC_TOKEN", Value: "/var/run/secrets/netci/token"},
					{Name: "NETCI_AGENT_JAR", Value: jar},
					{Name: "NETCI_WORKDIR", Value: "/home/jenkins/agent"},
					{Name: "NETCI_READY_FILE", Value: "/tmp/netci-ready"},
				},
				Resources:       corev1.ResourceRequirements{Requests: limits, Limits: limits},
				SecurityContext: restricted,
				// Ready once the bootstrap has reached the fabric: a warm sandbox is one that can
				// take a binding now, not merely a container that started.
				ReadinessProbe: &corev1.Probe{
					ProbeHandler:  corev1.ProbeHandler{Exec: &corev1.ExecAction{Command: []string{"/netci-bin/netci-sandbox", "ready"}}},
					PeriodSeconds: 1, FailureThreshold: 1,
				},
				VolumeMounts: []corev1.VolumeMount{
					{Name: "netci-bin", MountPath: "/netci-bin", ReadOnly: true},
					{Name: "identity", MountPath: "/var/run/secrets/netci", ReadOnly: true},
					{Name: "workspace", MountPath: "/home/jenkins/agent"},
					{Name: "tmp", MountPath: "/tmp"},
				},
			}},
			Volumes: []corev1.Volume{
				{Name: "netci-bin", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
				{Name: "workspace", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
				{Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
				{Name: "identity", VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
					Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
						Audience: ps.Audience, ExpirationSeconds: &expiry, Path: "token"}}},
				}}},
			},
		},
	}
	if p.RuntimeClass != "" {
		pod.Spec.RuntimeClassName = &p.RuntimeClass
	}
	if p.UserNamespace {
		pod.Spec.HostUsers = &no
	}
	return pod
}

func ptr[T any](v T) *T { return &v }

func pullSecrets(names []string) []corev1.LocalObjectReference {
	var out []corev1.LocalObjectReference
	for _, n := range names {
		out = append(out, corev1.LocalObjectReference{Name: n})
	}
	return out
}
