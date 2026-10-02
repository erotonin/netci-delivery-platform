package fabric

import (
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestFailedTransitionsExistAtZeroBeforeAnySandboxFails(t *testing.T) {
	r := prometheus.NewRegistry()
	NewMetrics(r).Known([]string{"default"})
	n, err := testutil.GatherAndCount(r, "netci_fabric_transitions_total")
	if err != nil || n != 6 {
		t.Fatalf("%d series (%v), want one per state", n, err)
	}
}
