package runqueue

import "github.com/prometheus/client_golang/prometheus"

// Metrics of the queue.
type Metrics struct {
	transitions   *prometheus.CounterVec
	redispatched  *prometheus.CounterVec
	errors        *prometheus.CounterVec
	acceptToStart *prometheus.HistogramVec
	accepted      *prometheus.CounterVec
}

// NewMetrics registers the queue's metrics on r.
func NewMetrics(r prometheus.Registerer) *Metrics {
	m := &Metrics{
		transitions: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "netci_runs_transitions_total",
			Help: "Run state transitions, by cell and new state."}, []string{"cell", "to"}),
		redispatched: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "netci_runs_redispatched_total",
			Help: "Runs dispatched again because the controller that had them queued restarted and lost its queue."}, []string{"cell"}),
		errors: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "netci_runs_controller_errors_total",
			Help: "Controller calls that failed and will be retried."}, []string{"cell"}),
		acceptToStart: prometheus.NewHistogramVec(prometheus.HistogramOpts{Name: "netci_runs_accept_to_start_seconds",
			Help: "From a run being accepted to its build existing.", Buckets: prometheus.ExponentialBuckets(0.25, 2, 14)}, []string{"cell"}),
		accepted: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "netci_runs_accepted_total",
			Help: "Runs accepted by intake, by cell and client."}, []string{"cell", "client"}),
	}
	r.MustRegister(m.transitions, m.redispatched, m.errors, m.acceptToStart, m.accepted)
	return m
}
