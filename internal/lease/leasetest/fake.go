// Package leasetest is an in-memory Lease API with what client-go's fake leaves out and the
// lease logic depends on: resourceVersion increments on every write, an update carrying a stale
// resourceVersion is a conflict, and the server can be made unreachable.
package leasetest

import (
	"context"
	"fmt"
	"strconv"
	"sync"

	coordinationv1 "k8s.io/api/coordination/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
)

// APIServer implements coordinationclient.LeasesGetter.
type APIServer struct {
	mu          sync.Mutex
	leases      map[string]*coordinationv1.Lease
	rv          int
	unreachable bool
	// BeforeWrite, if set, runs before every write is applied, outside the lock: a test steps a
	// fake clock in it to model a call that is slow to reach the server.
	BeforeWrite func()
}

// New returns an empty, reachable server.
func New() *APIServer { return &APIServer{leases: map[string]*coordinationv1.Lease{}} }

// SetUnreachable makes every call fail as a network error would.
func (a *APIServer) SetUnreachable(down bool) {
	a.mu.Lock()
	a.unreachable = down
	a.mu.Unlock()
}

func (a *APIServer) Leases(namespace string) coordinationclient.LeaseInterface {
	return &leaseClient{a: a, ns: namespace}
}

// Get returns a copy of the stored Lease, or nil.
func (a *APIServer) Get(ns, name string) *coordinationv1.Lease {
	a.mu.Lock()
	defer a.mu.Unlock()
	if l, ok := a.leases[ns+"/"+name]; ok {
		return l.DeepCopy()
	}
	return nil
}

var leaseResource = schema.GroupResource{Group: "coordination.k8s.io", Resource: "leases"}

type leaseClient struct {
	coordinationclient.LeaseInterface // unused methods panic
	a                                 *APIServer
	ns                                string
}

func (c *leaseClient) Get(ctx context.Context, name string, _ metav1.GetOptions) (*coordinationv1.Lease, error) {
	c.a.mu.Lock()
	defer c.a.mu.Unlock()
	if c.a.unreachable {
		return nil, fmt.Errorf("dial tcp: connection refused")
	}
	l, ok := c.a.leases[c.ns+"/"+name]
	if !ok {
		return nil, apierrors.NewNotFound(leaseResource, name)
	}
	return l.DeepCopy(), nil
}

func (c *leaseClient) Create(ctx context.Context, l *coordinationv1.Lease, _ metav1.CreateOptions) (*coordinationv1.Lease, error) {
	if hook := c.a.BeforeWrite; hook != nil {
		hook()
	}
	c.a.mu.Lock()
	defer c.a.mu.Unlock()
	if c.a.unreachable {
		return nil, fmt.Errorf("dial tcp: connection refused")
	}
	key := c.ns + "/" + l.Name
	if _, ok := c.a.leases[key]; ok {
		return nil, apierrors.NewAlreadyExists(leaseResource, l.Name)
	}
	c.a.rv++
	stored := l.DeepCopy()
	stored.Namespace, stored.ResourceVersion = c.ns, strconv.Itoa(c.a.rv)
	c.a.leases[key] = stored
	return stored.DeepCopy(), nil
}

func (c *leaseClient) Update(ctx context.Context, l *coordinationv1.Lease, _ metav1.UpdateOptions) (*coordinationv1.Lease, error) {
	if hook := c.a.BeforeWrite; hook != nil {
		hook()
	}
	c.a.mu.Lock()
	defer c.a.mu.Unlock()
	if c.a.unreachable {
		return nil, fmt.Errorf("dial tcp: connection refused")
	}
	key := c.ns + "/" + l.Name
	cur, ok := c.a.leases[key]
	if !ok {
		return nil, apierrors.NewNotFound(leaseResource, l.Name)
	}
	if l.ResourceVersion != cur.ResourceVersion {
		return nil, apierrors.NewConflict(leaseResource, l.Name, fmt.Errorf("stale resourceVersion"))
	}
	c.a.rv++
	stored := l.DeepCopy()
	stored.ResourceVersion = strconv.Itoa(c.a.rv)
	c.a.leases[key] = stored
	return stored.DeepCopy(), nil
}

// Unreachable is the API server as seen from a node cut off from it.
type Unreachable struct{}

func (Unreachable) Leases(string) coordinationclient.LeaseInterface { return unreachableClient{} }

type unreachableClient struct {
	coordinationclient.LeaseInterface
}

func (unreachableClient) Get(context.Context, string, metav1.GetOptions) (*coordinationv1.Lease, error) {
	return nil, fmt.Errorf("dial tcp: i/o timeout")
}
func (unreachableClient) Create(context.Context, *coordinationv1.Lease, metav1.CreateOptions) (*coordinationv1.Lease, error) {
	return nil, fmt.Errorf("dial tcp: i/o timeout")
}
func (unreachableClient) Update(context.Context, *coordinationv1.Lease, metav1.UpdateOptions) (*coordinationv1.Lease, error) {
	return nil, fmt.Errorf("dial tcp: i/o timeout")
}

func (c *leaseClient) List(ctx context.Context, _ metav1.ListOptions) (*coordinationv1.LeaseList, error) {
	c.a.mu.Lock()
	defer c.a.mu.Unlock()
	if c.a.unreachable {
		return nil, fmt.Errorf("dial tcp: i/o timeout")
	}
	out := &coordinationv1.LeaseList{}
	for _, l := range c.a.leases {
		if l.Namespace == c.ns {
			out.Items = append(out.Items, *l.DeepCopy())
		}
	}
	return out, nil
}
