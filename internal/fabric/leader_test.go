package fabric

import (
	"context"
	"testing"

	discoveryv1 "k8s.io/api/discovery/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func TestTheLeaderRoutesTheServiceToItselfAlone(t *testing.T) {
	kube := fake.NewClientset(&discoveryv1.EndpointSlice{
		ObjectMeta:  metav1.ObjectMeta{Name: LeaderSlice, Namespace: "netci-system", Labels: map[string]string{"kubernetes.io/service-name": "netci-fabric"}},
		AddressType: discoveryv1.AddressTypeIPv4,
	})
	ctx := context.Background()
	for _, ip := range []string{"10.42.0.7", "10.42.1.9"} { // a failover: the new leader replaces the old
		if err := PublishLeader(ctx, kube, "netci-system", ip); err != nil {
			t.Fatal(err)
		}
		s, _ := kube.DiscoveryV1().EndpointSlices("netci-system").Get(ctx, LeaderSlice, metav1.GetOptions{})
		if len(s.Endpoints) != 1 || s.Endpoints[0].Addresses[0] != ip || !*s.Endpoints[0].Conditions.Ready || *s.Ports[0].Port != 8080 {
			t.Fatalf("%+v", s)
		}
		if s.Labels["kubernetes.io/service-name"] != "netci-fabric" {
			t.Fatal("the slice no longer belongs to the Service")
		}
	}
}
