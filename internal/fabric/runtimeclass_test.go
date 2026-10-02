package fabric

import (
	"context"
	"strings"
	"testing"

	nodev1 "k8s.io/api/node/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func TestAPoolWhoseRuntimeClassIsMissingIsRefused(t *testing.T) {
	client := fake.NewSimpleClientset(&nodev1.RuntimeClass{ObjectMeta: metav1.ObjectMeta{Name: "kata-qemu-runtime-rs"}, Handler: "kata-qemu-runtime-rs"})
	ok := []Pool{{Name: "standard"}, {Name: "untrusted", RuntimeClass: "kata-qemu-runtime-rs"}}
	if err := CheckRuntimeClasses(context.Background(), client, ok); err != nil {
		t.Fatal(err)
	}
	missing := append(ok, Pool{Name: "gvisor", RuntimeClass: "gvisor"})
	err := CheckRuntimeClasses(context.Background(), client, missing)
	if err == nil || !strings.Contains(err.Error(), "pool gvisor asks for RuntimeClass gvisor") {
		t.Fatalf("a missing RuntimeClass must be refused, got %v", err)
	}
}
