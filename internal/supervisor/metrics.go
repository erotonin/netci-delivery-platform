package supervisor

import "github.com/prometheus/client_golang/prometheus"

// Metrics of the supervisor.
type Metrics struct {
	actions         *prometheus.CounterVec
	alerts          prometheus.Counter
	leaseMoved      prometheus.Counter
	observeErrors   prometheus.Counter
	resets          prometheus.Counter
	fenceSeconds    prometheus.Histogram
	takeoverSeconds prometheus.Histogram
	cells           prometheus.Gauge
	lost            prometheus.Gauge
	leading         prometheus.Gauge
	headroom        *prometheus.GaugeVec
	sharing         prometheus.Gauge
}

// NewMetrics registers the supervisor's metrics on r.
func NewMetrics(r prometheus.Registerer) *Metrics {
	m := &Metrics{
		actions: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "netci_supervisor_actions_total",
			Help: "Actions carried out, by kind and result."}, []string{"kind", "result"}),
		alerts: prometheus.NewCounter(prometheus.CounterOpts{Name: "netci_supervisor_alerts_total",
			Help: "Decisions that needed a person and did nothing."}),
		leaseMoved: prometheus.NewCounter(prometheus.CounterOpts{Name: "netci_supervisor_lease_moved_total",
			Help: "Fencings stopped because the lease changed after its machine was confirmed off (a wrong node-to-machine mapping)."}),
		observeErrors: prometheus.NewCounter(prometheus.CounterOpts{Name: "netci_supervisor_observe_errors_total",
			Help: "Observations of the cluster that failed; no decision is made on them."}),
		resets: prometheus.NewCounter(prometheus.CounterOpts{Name: "netci_supervisor_observation_resets_total",
			Help: "Times the supervisor forgot what it had seen after a gap in its observations."}),
		fenceSeconds: prometheus.NewHistogram(prometheus.HistogramOpts{Name: "netci_supervisor_fence_seconds",
			Help: "From the decision to fence to the pod deleted.", Buckets: []float64{0.5, 1, 2, 3, 5, 10, 20, 30, 60}}),
		takeoverSeconds: prometheus.NewHistogram(prometheus.HistogramOpts{Name: "netci_supervisor_takeover_seconds",
			Help:    "From the last renewal seen by the supervisor to a new pod holding the cell's lease.",
			Buckets: []float64{5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300}}),
		cells:   prometheus.NewGauge(prometheus.GaugeOpts{Name: "netci_supervisor_cells", Help: "Cells observed."}),
		lost:    prometheus.NewGauge(prometheus.GaugeOpts{Name: "netci_supervisor_cells_lost", Help: "Cells whose pod has stopped renewing its lease."}),
		leading: prometheus.NewGauge(prometheus.GaugeOpts{Name: "netci_supervisor_leading", Help: "1 while this replica is the one acting."}),
		headroom: prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "netci_supervisor_cell_headroom",
			Help: "0 when no other machine could take the cell's controller if its machine were lost."}, []string{"cell"}),
		sharing: prometheus.NewGauge(prometheus.GaugeOpts{Name: "netci_supervisor_cells_sharing_a_machine",
			Help: "Cells whose controller shares its machine with another cell's: lost together with it."}),
	}
	r.MustRegister(m.actions, m.alerts, m.leaseMoved, m.observeErrors, m.resets, m.fenceSeconds, m.takeoverSeconds, m.cells, m.lost, m.leading,
		m.headroom, m.sharing)
	return m
}

// SetLeading records whether this replica is the one acting.
func (m *Metrics) SetLeading(on bool) {
	if on {
		m.leading.Set(1)
		return
	}
	m.leading.Set(0)
}

// SetHeadroom records whether the cell's controller could be placed elsewhere.
func (m *Metrics) SetHeadroom(cell string, fits bool) {
	v := 0.0
	if fits {
		v = 1
	}
	m.headroom.WithLabelValues(cell).Set(v)
}

// SetSharing records how many cells share a machine with another cell.
func (m *Metrics) SetSharing(n int) { m.sharing.Set(float64(n)) }
