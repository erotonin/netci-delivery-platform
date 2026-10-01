package kubeclient

import (
	"context"
	"log/slog"
	"net"
	"slices"
	"strconv"
	"sync"
	"time"

	discoveryv1 "k8s.io/api/discovery/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// Balancer chooses which API server a new connection goes to: in turn, avoiding for Avoid one
// that just failed an attempt. When every one is being avoided, the one whose avoidance ends
// first is tried: a client that refuses every server would be blind for no reason.
type Balancer struct {
	Avoid time.Duration
	Now   func() time.Time // tests set it

	mu    sync.Mutex
	addrs []string
	until map[string]time.Time
	next  int
}

// NewBalancer avoids a failed API server for avoid.
func NewBalancer(avoid time.Duration) *Balancer {
	return &Balancer{Avoid: avoid, Now: time.Now, until: map[string]time.Time{}}
}

// Set replaces the API servers ("ip:port"). An empty list is ignored: losing the list is no
// reason to forget it.
func (b *Balancer) Set(addrs []string) {
	if len(addrs) == 0 {
		return
	}
	addrs = slices.Clone(addrs)
	slices.Sort(addrs)
	addrs = slices.Compact(addrs)
	b.mu.Lock()
	defer b.mu.Unlock()
	b.addrs = addrs
}

// Addrs returns the API servers known.
func (b *Balancer) Addrs() []string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return slices.Clone(b.addrs)
}

func (b *Balancer) pick() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.addrs) == 0 {
		return ""
	}
	now := b.Now()
	for i := 0; i < len(b.addrs); i++ {
		a := b.addrs[(b.next+i)%len(b.addrs)]
		if !now.Before(b.until[a]) {
			b.next = (b.next + i + 1) % len(b.addrs)
			return a
		}
	}
	soonest := b.addrs[0]
	for _, a := range b.addrs[1:] {
		if b.until[a].Before(b.until[soonest]) {
			soonest = a
		}
	}
	return soonest
}

func (b *Balancer) fail(addr string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.until[addr] = b.Now().Add(b.Avoid)
}

// Discover keeps b's list of API servers from the endpoints of the default kubernetes Service,
// every interval until ctx is done. It needs list on endpointslices in the default namespace;
// without it b stays empty and connections go to the Service.
func Discover(ctx context.Context, client kubernetes.Interface, b *Balancer, every time.Duration, log *slog.Logger) {
	warned := false
	for {
		found, err := client.DiscoveryV1().EndpointSlices(metav1.NamespaceDefault).List(ctx,
			metav1.ListOptions{LabelSelector: discoveryv1.LabelServiceName + "=kubernetes"})
		switch {
		case err != nil && !warned && ctx.Err() == nil:
			log.Warn("API servers not discovered; connections go through the Service", "error", err)
			warned = true
		case err == nil:
			b.Set(apiServers(found.Items))
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(every):
		}
	}
}

func apiServers(items []discoveryv1.EndpointSlice) []string {
	var out []string
	for _, s := range items {
		port := int32(0)
		for _, p := range s.Ports {
			if p.Port != nil && (p.Name == nil || *p.Name == "https" || port == 0) {
				port = *p.Port
			}
		}
		if port == 0 {
			continue
		}
		for _, e := range s.Endpoints {
			if e.Conditions.Ready != nil && !*e.Conditions.Ready {
				continue
			}
			for _, ip := range e.Addresses {
				out = append(out, net.JoinHostPort(ip, strconv.Itoa(int(port))))
			}
		}
	}
	return out
}
