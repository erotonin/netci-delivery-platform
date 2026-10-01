package fabric

import (
	"context"

	corev1 "k8s.io/api/core/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/util/retry"
)

// LeaderSlice is the EndpointSlice of the netci-fabric Service (which has no selector). The
// leader points it at itself: claims and bindings reach the one replica holding them in memory,
// and a standby replica is still an ordinary ready pod (a Deployment rollout completes).
const LeaderSlice = "netci-fabric-leader"

// PublishLeader points the Service at ip.
func PublishLeader(ctx context.Context, client kubernetes.Interface, namespace, ip string) error {
	ready, port, proto, name := true, int32(8080), corev1.ProtocolTCP, "http"
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		s, err := client.DiscoveryV1().EndpointSlices(namespace).Get(ctx, LeaderSlice, metav1.GetOptions{})
		if err != nil {
			return err
		}
		s.AddressType = discoveryv1.AddressTypeIPv4
		s.Endpoints = []discoveryv1.Endpoint{{Addresses: []string{ip}, Conditions: discoveryv1.EndpointConditions{Ready: &ready}}}
		s.Ports = []discoveryv1.EndpointPort{{Name: &name, Port: &port, Protocol: &proto}}
		_, err = client.DiscoveryV1().EndpointSlices(namespace).Update(ctx, s, metav1.UpdateOptions{})
		return err
	})
}
