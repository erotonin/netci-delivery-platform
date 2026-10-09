package supervisor

import (
	"context"
	"fmt"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	clocktesting "k8s.io/utils/clock/testing"

	"github.com/erotonin/netci-delivery-platform/internal/lease/leasetest"
)

// What the scale test (lab/scale) found: one GET of a Lease and one of a pod per cell made an
// observation take 4 s at 100 cells. An observation must cost the same whatever the cell count.
func TestAnObservationCostsTheSameForOneCellAndForFifty(t *testing.T) {
	requests := func(cells int) (pods, leases int, seen int) {
		var objs []runtime.Object
		srv := leasetest.New()
		holder, seconds := "", int32(15)
		for i := 0; i < cells; i++ {
			ns := fmt.Sprintf("cell-%02d", i)
			objs = append(objs,
				&appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: "jenkins", Namespace: ns, Labels: map[string]string{CellLabel: "true"}}},
				&corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "jenkins-0", Namespace: ns, UID: "u"}, Spec: corev1.PodSpec{NodeName: "n1"}})
			holder = "jenkins-0/u"
			if _, err := srv.Leases(ns).Create(context.Background(), &coordinationv1.Lease{ObjectMeta: metav1.ObjectMeta{Name: "jenkins", Namespace: ns},
				Spec: coordinationv1.LeaseSpec{HolderIdentity: &holder, LeaseDurationSeconds: &seconds}}, metav1.CreateOptions{}); err != nil {
				t.Fatal(err)
			}
		}
		kube := fake.NewClientset(objs...)
		c := &Collector{Client: kube, Leases: srv, Config: DefaultConfig(), Clock: clocktesting.NewFakeClock(time.Unix(1_000_000, 0)),
			MaxGap: 3 * time.Second, StateTimeout: time.Second}
		before := srv.Calls()
		snap, err := c.Collect(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		for _, a := range kube.Actions() {
			if a.GetResource().Resource == "pods" {
				pods++
			}
		}
		return pods, srv.Calls() - before, len(snap.Input.Cells)
	}
	p1, l1, s1 := requests(1)
	p50, l50, s50 := requests(50)
	if s1 != 1 || s50 != 50 {
		t.Fatalf("cells seen: %d, %d", s1, s50)
	}
	if p50 != p1 || l50 != l1 {
		t.Fatalf("requests grow with cells: pods %d -> %d, leases %d -> %d", p1, p50, l1, l50)
	}
}
