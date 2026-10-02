package fabric

import "github.com/prometheus/client_golang/prometheus"

// Metrics of the fabric.
type Metrics struct {
	sandboxes   *prometheus.GaugeVec
	transitions *prometheus.CounterVec
	claims      *prometheus.CounterVec
	claimToBind *prometheus.HistogramVec
	busySeconds *prometheus.HistogramVec
}

// NewMetrics registers the fabric's metrics on r.
func NewMetrics(r prometheus.Registerer) *Metrics {
	m := &Metrics{
		sandboxes: prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "netci_fabric_sandboxes",
			Help: "Sandboxes by pool and state."}, []string{"pool", "state"}),
		transitions: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "netci_fabric_transitions_total",
			Help: "Sandbox state transitions made by the reconciler."}, []string{"pool", "to"}),
		claims: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "netci_fabric_claims_total",
			Help: "Claims served, from a warm sandbox or a cold one created for the claim."}, []string{"pool", "kind"}),
		claimToBind: prometheus.NewHistogramVec(prometheus.HistogramOpts{Name: "netci_fabric_claim_to_bind_seconds",
			Help: "From a controller's claim to the sandbox taking its binding.", Buckets: prometheus.ExponentialBuckets(0.05, 2, 14)}, []string{"pool"}),
		busySeconds: prometheus.NewHistogramVec(prometheus.HistogramOpts{Name: "netci_fabric_busy_seconds",
			Help: "How long a sandbox served its build.", Buckets: prometheus.ExponentialBuckets(1, 2, 14)}, []string{"pool"}),
	}
	r.MustRegister(m.sandboxes, m.transitions, m.claims, m.claimToBind, m.busySeconds)
	return m
}

// Known starts every series at 0 for the configured pools: a labelled counter or histogram has no
// series until its first observation, so increase() and rate() would miss the first sandboxes
// that fail and the first claims served after a start.
func (m *Metrics) Known(pools []string) {
	for _, p := range pools {
		for _, s := range []State{Creating, Warm, Claimed, Bound, Released, Failed} {
			m.transitions.WithLabelValues(p, string(s))
		}
		m.claims.WithLabelValues(p, "warm")
		m.claims.WithLabelValues(p, "cold")
		m.claimToBind.WithLabelValues(p)
		m.busySeconds.WithLabelValues(p)
	}
}

func (m *Metrics) setPool(pool string, counts map[State]int) {
	for _, s := range []State{Creating, Warm, Claimed, Bound, Released, Failed} {
		m.sandboxes.WithLabelValues(pool, string(s)).Set(float64(counts[s]))
	}
}
