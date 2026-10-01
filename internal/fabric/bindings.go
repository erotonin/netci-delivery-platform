package fabric

import (
	"sync"

	"github.com/google/uuid"
)

// Binding is what a sandbox needs to become a controller's agent. It holds the agent's inbound
// secret, so it lives in this process's memory only: never in the database, never in a log.
type Binding struct {
	Controller string `json:"controller"`
	Agent      string `json:"agent"`
	Secret     string `json:"secret"`
}

// Bindings are the claims' bindings, and a way for a waiting sandbox to be woken when its
// binding arrives.
type Bindings struct {
	mu      sync.Mutex
	by      map[uuid.UUID]Binding
	waiters map[uuid.UUID]chan struct{}
}

// NewBindings returns an empty set.
func NewBindings() *Bindings {
	return &Bindings{by: map[uuid.UUID]Binding{}, waiters: map[uuid.UUID]chan struct{}{}}
}

// Put stores a binding and wakes the sandbox if it is waiting.
func (b *Bindings) Put(id uuid.UUID, bd Binding) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.by[id] = bd
	if w, ok := b.waiters[id]; ok {
		close(w)
		delete(b.waiters, id)
	}
}

// Get returns the binding, if any.
func (b *Bindings) Get(id uuid.UUID) (Binding, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	bd, ok := b.by[id]
	return bd, ok
}

// Wait returns a channel closed when a binding for id arrives.
func (b *Bindings) Wait(id uuid.UUID) <-chan struct{} {
	b.mu.Lock()
	defer b.mu.Unlock()
	if _, ok := b.by[id]; ok {
		c := make(chan struct{})
		close(c)
		return c
	}
	w, ok := b.waiters[id]
	if !ok {
		w = make(chan struct{})
		b.waiters[id] = w
	}
	return w
}

// Drop forgets a binding (released, failed, or given up) and wakes anyone waiting for it, who
// then finds it gone.
func (b *Bindings) Drop(id uuid.UUID) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.by, id)
	if w, ok := b.waiters[id]; ok {
		close(w)
		delete(b.waiters, id)
	}
}
