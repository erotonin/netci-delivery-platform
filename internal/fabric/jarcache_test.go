package fabric

import (
	"strings"
	"testing"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
)

func TestAPoolsJarCacheIsMountedReadOnlyAndNamedToTheSandbox(t *testing.T) {
	const image = "registry.example/netci/jenkins-agent-jars@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	p := Pool{Name: "standard", Labels: []string{"netci-standard"}, Image: "agent", Warm: 1, Max: 2,
		CPU: "1", Memory: "1Gi", Disk: "4Gi", JarCache: image}
	if err := p.validate(); err != nil {
		t.Fatal(err)
	}
	pod := PodSettings{Namespace: "netci-agents", BootstrapImage: "netci@sha256:x"}.pod(&Sandbox{ID: uuid.New(), Pod: "sbx-1", Pool: "standard"}, p)
	var vol *corev1.Volume
	for i := range pod.Spec.Volumes {
		if pod.Spec.Volumes[i].Name == "jar-cache" {
			vol = &pod.Spec.Volumes[i]
		}
	}
	if vol == nil || vol.Image == nil || vol.Image.Reference != image {
		t.Fatalf("no image volume of the pool's jar cache: %+v", pod.Spec.Volumes)
	}
	agent := pod.Spec.Containers[0]
	mounted, env := false, ""
	for _, m := range agent.VolumeMounts {
		if m.Name == "jar-cache" {
			mounted = m.ReadOnly && m.MountPath == jarCacheMount
		}
	}
	for _, e := range agent.Env {
		if e.Name == "NETCI_JAR_CACHE" {
			env = e.Value
		}
	}
	if !mounted || env != jarCacheMount+"/jarCache" {
		t.Fatalf("the jar cache must be mounted read-only and named to the bootstrap: mounts %+v, env %q", agent.VolumeMounts, env)
	}
	// Without one, nothing changes.
	p.JarCache = ""
	for _, v := range (PodSettings{}).pod(&Sandbox{ID: uuid.New(), Pod: "sbx-2"}, p).Spec.Volumes {
		if v.Name == "jar-cache" {
			t.Fatal("a pool without a jar cache has none")
		}
	}
}

// The agent loads a jar from its cache by name and never checks its content: a tag could be moved
// to an image whose jars are not the ones their names say.
func TestAJarCacheNotPinnedByDigestIsRefused(t *testing.T) {
	p := Pool{Name: "standard", Labels: []string{"netci-standard"}, Image: "agent", Warm: 1, Max: 2,
		CPU: "1", Memory: "1Gi", Disk: "4Gi", JarCache: "registry.example/netci/jenkins-agent-jars:2.555.3"}
	if err := p.validate(); err == nil || !strings.Contains(err.Error(), "pinned by digest") {
		t.Fatalf("a jar cache by tag must be refused, got %v", err)
	}
}
