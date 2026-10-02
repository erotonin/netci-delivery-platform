package runqueue

import (
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestTheAlertedCountersExistAtZeroBeforeAnyEvent(t *testing.T) {
	r := prometheus.NewRegistry()
	NewMetrics(r).Known([]string{"cell-a", "cell-b"}, []string{"ci", "gitlab"})
	want := `
# HELP netci_runs_redispatched_total Runs dispatched again because the controller that had them queued restarted and lost its queue.
# TYPE netci_runs_redispatched_total counter
netci_runs_redispatched_total{cell="cell-a"} 0
netci_runs_redispatched_total{cell="cell-b"} 0
`
	if err := testutil.GatherAndCompare(r, strings.NewReader(want), "netci_runs_redispatched_total"); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]int{"netci_runs_controller_errors_total": 2, "netci_runs_accepted_total": 4,
		"netci_runs_accept_to_start_seconds": 2} {
		if n, err := testutil.GatherAndCount(r, name); err != nil || n != want {
			t.Fatalf("%s: %d series (%v), want %d", name, n, err, want)
		}
	}
}
