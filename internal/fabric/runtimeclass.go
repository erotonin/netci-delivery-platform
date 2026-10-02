package fabric

import (
	"context"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// CheckRuntimeClasses refuses a configuration whose pools ask for a RuntimeClass the cluster
// does not have. Without it the API server refuses each of that pool's pods, the pool never has
// a sandbox, and the only sign is a warm-sandbox alert minutes later. There is no fallback to a
// weaker runtime: a pool for untrusted code that ran it on the shared kernel would be the
// isolation it promises, silently not given. A start that fails on this leaves the previous
// replicas serving, since a rolling update waits for the new ones.
func CheckRuntimeClasses(ctx context.Context, client kubernetes.Interface, pools []Pool) error {
	for _, p := range pools {
		if p.RuntimeClass == "" {
			continue
		}
		_, err := client.NodeV1().RuntimeClasses().Get(ctx, p.RuntimeClass, metav1.GetOptions{})
		switch {
		case err == nil:
		case apierrors.IsNotFound(err):
			return fmt.Errorf("pool %s asks for RuntimeClass %s, which this cluster does not have: install its runtime (for Kata, kata-deploy) or remove the pool", p.Name, p.RuntimeClass)
		default:
			return fmt.Errorf("pool %s: checking RuntimeClass %s: %w", p.Name, p.RuntimeClass, err)
		}
	}
	return nil
}
