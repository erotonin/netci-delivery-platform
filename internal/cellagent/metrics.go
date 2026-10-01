package cellagent

import "github.com/prometheus/client_golang/prometheus"

// Metrics of one cell agent.
type Metrics struct {
	holding prometheus.Gauge
	epoch   prometheus.Gauge
	lost    prometheus.Counter
}

// NewMetrics registers the agent's metrics on r.
func NewMetrics(r prometheus.Registerer) *Metrics {
	m := &Metrics{
		holding: prometheus.NewGauge(prometheus.GaugeOpts{Name: "netci_cell_lease_held", Help: "1 while this pod holds the cell's lease and the controller may run."}),
		epoch:   prometheus.NewGauge(prometheus.GaugeOpts{Name: "netci_cell_lease_epoch", Help: "Epoch (lease transitions) of the lease held."}),
		lost:    prometheus.NewCounter(prometheus.CounterOpts{Name: "netci_cell_lease_lost_total", Help: "Times the lease was lost and the controller killed."}),
	}
	r.MustRegister(m.holding, m.epoch, m.lost)
	return m
}

func (m *Metrics) setHolding(held bool, epoch int32) {
	if held {
		m.holding.Set(1)
		m.epoch.Set(float64(epoch))
		return
	}
	m.holding.Set(0)
}
