package fabric

import (
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestEverySeriesExistsAtZeroBeforeAnySandboxIsUsed(t *testing.T) {
	r := prometheus.NewRegistry()
	NewMetrics(r).Known([]string{"default"})
	for name, want := range map[string]int{"netci_fabric_transitions_total": 6, "netci_fabric_claims_total": 2,
		"netci_fabric_claim_to_bind_seconds": 1, "netci_fabric_busy_seconds": 1} {
		if n, err := testutil.GatherAndCount(r, name); err != nil || n != want {
			t.Fatalf("%s: %d series (%v), want %d", name, n, err, want)
		}
	}
}
