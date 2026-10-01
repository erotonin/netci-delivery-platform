package supervisor

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/erotonin/netci-delivery-platform/internal/fence"
)

var now = time.Unix(100000, 0)

// world: three control-plane nodes, all alive, machines running; cell-a's pod on node 1
// holding its Lease.
func world() Input {
	nodes := map[string]NodeView{}
	for i := 1; i <= 3; i++ {
		name := fmt.Sprintf("netci-lab-%d", i)
		nodes[name] = NodeView{Name: name, Machine: name, Ready: true, ControlPlane: true, KubeletFresh: true, MachineState: fence.Running}
	}
	return Input{Now: now, Nodes: nodes, Cells: []CellView{cell("cell-a", "netci-lab-1")}}
}

func cell(ns, node string) CellView {
	return CellView{Namespace: ns, Name: "jenkins", Pod: "jenkins-0", PodUID: "uid-" + ns, PodExists: true, PodNode: node,
		LeaseHolder: "jenkins-0/uid-" + ns, LeaseUnchanged: time.Second}
}

func expire(c *CellView, unchanged time.Duration) {
	c.LeaseExpired, c.LeaseUnchanged = true, unchanged
}

func node(in Input, name string, f func(*NodeView)) {
	n := in.Nodes[name]
	f(&n)
	in.Nodes[name] = n
}

func only(t *testing.T, got []Action, kind Kind) Action {
	t.Helper()
	if len(got) != 1 || got[0].Kind != kind {
		t.Fatalf("want one %s, got %+v", kind, got)
	}
	return got[0]
}

func none(t *testing.T, got []Action) {
	t.Helper()
	if len(got) != 0 {
		t.Fatalf("want nothing, got %+v", got)
	}
}

func cfg() Config { return DefaultConfig() }

func TestNothingHappensWhileTheLeaseIsRenewed(t *testing.T) {
	none(t, Decide(world(), cfg()))
}

func TestAPowerLossIsFencedAtOnceEvenThoughTheKubeletStillLooksAlive(t *testing.T) {
	// 6 s after a power loss the kubelet's last heartbeat is younger than NodeStale. Waiting for
	// it would cost up to 20 s; the power controller already knows.
	in := world()
	expire(&in.Cells[0], 6*time.Second)
	node(in, "netci-lab-1", func(n *NodeView) { n.MachineState = fence.Off })
	a := only(t, Decide(in, cfg()), FenceNode)
	if a.PowerOff || a.Node != "netci-lab-1" || a.Machine != "netci-lab-1" || a.Holder != "jenkins-0/uid-cell-a" {
		t.Fatalf("%+v", a)
	}
}

func TestARunningMachineWithALiveKubeletIsGivenTimeBeforeItsPodIsRestarted(t *testing.T) {
	in := world()
	expire(&in.Cells[0], 10*time.Second)
	none(t, Decide(in, cfg()))
	in.Cells[0].LeaseUnchanged = cfg().StuckPodAfter
	a := only(t, Decide(in, cfg()), DeletePod)
	if a.Pod != "jenkins-0" || a.PodUID != "uid-cell-a" {
		t.Fatalf("%+v", a)
	}
}

func TestAPodAlreadyBeingDeletedOnALiveNodeIsLeftToTheKubelet(t *testing.T) {
	in := world()
	expire(&in.Cells[0], time.Hour)
	in.Cells[0].PodDeleting = true
	none(t, Decide(in, cfg()))
}

func TestAHungMachineIsPoweredOffOnceItsKubeletStopsHeartbeating(t *testing.T) {
	in := world()
	expire(&in.Cells[0], 25*time.Second)
	node(in, "netci-lab-1", func(n *NodeView) { n.KubeletFresh = false })
	a := only(t, Decide(in, cfg()), FenceNode)
	if !a.PowerOff {
		t.Fatalf("a running machine must be powered off: %+v", a)
	}
}

func TestAnUnknownPowerStateIsNeverTreatedAsOff(t *testing.T) {
	in := world()
	expire(&in.Cells[0], time.Minute)
	node(in, "netci-lab-1", func(n *NodeView) { n.KubeletFresh, n.MachineState = false, fence.Unknown })
	a := only(t, Decide(in, cfg()), Alert)
	if !strings.Contains(a.Reason, "unknown") {
		t.Fatal(a.Reason)
	}
}

func TestANodeWithoutAPowerControllerIsNeverFenced(t *testing.T) {
	in := world()
	expire(&in.Cells[0], time.Minute)
	node(in, "netci-lab-1", func(n *NodeView) { n.Machine, n.MachineState, n.KubeletFresh = "", fence.Off, false })
	only(t, Decide(in, cfg()), Alert)
}

func TestAReleasedLeaseIsNotALostOne(t *testing.T) {
	// The supervisor released it after fencing, or the pod shut down gracefully: the
	// replacement pod is starting and must not be deleted for not holding it yet.
	in := world()
	in.Cells[0].LeaseHolder = ""
	expire(&in.Cells[0], time.Hour)
	none(t, Decide(in, cfg()))
}

func TestALeaseLeftByAnEarlierPodIsLeftToItsSuccessor(t *testing.T) {
	in := world()
	in.Cells[0].LeaseHolder = "jenkins-0/uid-of-a-pod-that-is-gone"
	expire(&in.Cells[0], time.Hour)
	node(in, "netci-lab-1", func(n *NodeView) { n.KubeletFresh = false })
	none(t, Decide(in, cfg()))
}

func TestAnUnscheduledPodHasNothingToFence(t *testing.T) {
	in := world()
	expire(&in.Cells[0], time.Hour)
	in.Cells[0].PodNode = ""
	none(t, Decide(in, cfg()))
}

func TestAnUnexpiredLeaseIsNeverActedOnWhateverTheNodeLooksLike(t *testing.T) {
	in := world()
	node(in, "netci-lab-1", func(n *NodeView) { n.KubeletFresh, n.Ready, n.MachineState = false, false, fence.Off })
	none(t, Decide(in, cfg()))
}

func TestPoweringOffAControlPlaneNodeThatWouldLoseTheMajorityIsRefused(t *testing.T) {
	in := world()
	expire(&in.Cells[0], time.Minute)
	node(in, "netci-lab-1", func(n *NodeView) { n.KubeletFresh = false })
	node(in, "netci-lab-2", func(n *NodeView) { n.Fenced, n.MachineState, n.KubeletFresh, n.Ready = true, fence.Off, false, false })
	a := only(t, Decide(in, cfg()), Alert)
	if !strings.Contains(a.Reason, "majority") {
		t.Fatal(a.Reason)
	}
	// A machine that is already off can still be fenced: that changes nothing about quorum.
	node(in, "netci-lab-1", func(n *NodeView) { n.MachineState = fence.Off })
	only(t, Decide(in, cfg()), FenceNode)
}

func manyCells(n int) Input {
	in := world()
	in.Cells = nil
	for i := 0; i < n; i++ {
		in.Cells = append(in.Cells, cell(fmt.Sprintf("cell-%d", i), fmt.Sprintf("netci-lab-%d", i%3+1)))
	}
	return in
}

func TestWhenMostCellsStopAtOnceNothingRunningIsPoweredOff(t *testing.T) {
	in := manyCells(4)
	for i := 0; i < 3; i++ {
		expire(&in.Cells[i], time.Minute)
	}
	node(in, "netci-lab-1", func(n *NodeView) { n.KubeletFresh = false })
	got := Decide(in, cfg())
	for _, a := range got {
		if a.Kind == DeletePod || (a.Kind == FenceNode && a.PowerOff) {
			t.Fatalf("acted during a mass failure: %+v", got)
		}
	}
	if len(got) == 0 || got[0].Kind != Alert {
		t.Fatalf("no alert: %+v", got)
	}
}

func TestWhenMostNodesStopHeartbeatingAtOnceNothingRunningIsPoweredOff(t *testing.T) {
	in := world()
	expire(&in.Cells[0], time.Minute)
	node(in, "netci-lab-1", func(n *NodeView) { n.KubeletFresh = false })
	node(in, "netci-lab-2", func(n *NodeView) { n.KubeletFresh = false })
	got := Decide(in, cfg())
	for _, a := range got {
		if a.Kind != Alert {
			t.Fatalf("acted while most nodes were silent: %+v", got)
		}
	}
	if len(got) == 0 || !strings.Contains(got[0].Reason, "2 of 3 nodes") {
		t.Fatalf("%+v", got)
	}
}

func TestMachinesThatAreOffAreFencedEvenDuringAMassFailure(t *testing.T) {
	// A rack losing power: every machine is confirmed off, so fencing them is safe and is the
	// only way their cells come back.
	in := manyCells(3)
	for i := range in.Cells {
		expire(&in.Cells[i], time.Minute)
	}
	for _, name := range []string{"netci-lab-1", "netci-lab-2"} {
		node(in, name, func(n *NodeView) { n.KubeletFresh, n.MachineState = false, fence.Off })
	}
	var fenced int
	for _, a := range Decide(in, cfg()) {
		if a.Kind == FenceNode {
			if a.PowerOff {
				t.Fatalf("powered off a running machine during a mass failure: %+v", a)
			}
			fenced++
		}
	}
	if fenced != 2 {
		t.Fatalf("fenced %d of the 2 machines that are off", fenced)
	}
}

func TestOnlyOneRunningMachineIsPoweredOffPerDecision(t *testing.T) {
	in := manyCells(5) // 2 of 5 is under the panic fraction
	in.Nodes["netci-lab-4"] = NodeView{Name: "netci-lab-4", Machine: "netci-lab-4", Ready: true, KubeletFresh: true, MachineState: fence.Running}
	in.Nodes["netci-lab-5"] = NodeView{Name: "netci-lab-5", Machine: "netci-lab-5", Ready: true, KubeletFresh: true, MachineState: fence.Running}
	in.Cells[3].PodNode, in.Cells[4].PodNode = "netci-lab-4", "netci-lab-5"
	expire(&in.Cells[3], time.Minute)
	expire(&in.Cells[4], time.Minute)
	node(in, "netci-lab-4", func(n *NodeView) { n.KubeletFresh = false })
	node(in, "netci-lab-5", func(n *NodeView) { n.KubeletFresh = false })
	a := only(t, Decide(in, cfg()), FenceNode)
	if a.Node != "netci-lab-4" {
		t.Fatalf("not deterministic: %+v", a)
	}
}

func TestTheCooldownStopsAPowerOffLoop(t *testing.T) {
	in := world()
	expire(&in.Cells[0], time.Minute)
	node(in, "netci-lab-1", func(n *NodeView) { n.KubeletFresh = false })
	in.Cells[0].LastAction = now.Add(-30 * time.Second)
	only(t, Decide(in, cfg()), Alert)
	in.Cells[0].LastAction = now.Add(-cfg().Cooldown)
	only(t, Decide(in, cfg()), FenceNode)

	// The cooldown never holds back fencing a machine that is already off.
	in.Cells[0].LastAction = now.Add(-time.Second)
	node(in, "netci-lab-1", func(n *NodeView) { n.MachineState = fence.Off })
	only(t, Decide(in, cfg()), FenceNode)
}

func TestACellOnAFencedNodeIsLeftToRecovery(t *testing.T) {
	in := world()
	expire(&in.Cells[0], time.Minute)
	node(in, "netci-lab-1", func(n *NodeView) { n.Fenced, n.MachineState, n.KubeletFresh, n.CellPods = true, fence.Off, false, 1 })
	only(t, Decide(in, cfg()), ForceDeletePods)
}

func TestRecoveryOfAFencedNode(t *testing.T) {
	in := world()
	in.Cells = nil
	fenced := func(f func(*NodeView)) Input {
		node(in, "netci-lab-1", func(n *NodeView) {
			*n = NodeView{Name: "netci-lab-1", Machine: "netci-lab-1", ControlPlane: true, Fenced: true, FencedFor: time.Minute, MachineState: fence.Off}
			f(n)
		})
		return in
	}
	c := cfg()
	none(t, Decide(fenced(func(*NodeView) {}), c)) // stays fenced and off without AutoPowerOn
	c.AutoPowerOn = true
	only(t, Decide(fenced(func(*NodeView) {}), c), PowerOn)
	none(t, Decide(fenced(func(n *NodeView) { n.Attachments = 1 }), c))
	none(t, Decide(fenced(func(n *NodeView) { n.FencedFor = time.Second }), c))
	only(t, Decide(fenced(func(n *NodeView) { n.CellPods = 2 }), c), ForceDeletePods)
	none(t, Decide(fenced(func(n *NodeView) { n.MachineState = fence.Unknown }), c))
	none(t, Decide(fenced(func(n *NodeView) { n.MachineState = fence.Running }), c)) // booting: not Ready yet
	only(t, Decide(fenced(func(n *NodeView) { n.MachineState, n.Ready, n.KubeletFresh = fence.Running, true, true }), c), Unfence)
}

func TestAFencedMachineStaysOffUntilEveryCellIsHeldAgain(t *testing.T) {
	in := world()
	in.Cells[0].LeaseHolder, in.Cells[0].AwaitingTakeover = "", true // released for the fenced holder
	node(in, "netci-lab-1", func(n *NodeView) {
		*n = NodeView{Name: "netci-lab-1", Machine: "netci-lab-1", ControlPlane: true, Fenced: true, FencedFor: time.Hour, MachineState: fence.Off}
	})
	c := cfg()
	c.AutoPowerOn = true
	for _, a := range Decide(in, c) {
		if a.Kind == PowerOn {
			t.Fatal("powered a fenced machine on while a cell's takeover was still under way")
		}
	}
	in.Cells[0].LeaseHolder, in.Cells[0].AwaitingTakeover = "jenkins-0/uid-new", false
	in.Cells[0].PodUID = "uid-new"
	only(t, Decide(in, c), PowerOn)
}

// A replacement no node can take moves no volume, and the fenced machine may be the only room
// left: waiting for the cell to be held would wait forever (seen in the lab).
func TestACellWithNowhereToGoDoesNotHoldItsMachineOff(t *testing.T) {
	in := world()
	in.Cells[0].LeaseHolder, in.Cells[0].AwaitingTakeover, in.Cells[0].PodNode = "", true, ""
	node(in, "netci-lab-1", func(n *NodeView) {
		*n = NodeView{Name: "netci-lab-1", Machine: "netci-lab-1", ControlPlane: true, Fenced: true, FencedFor: time.Hour, MachineState: fence.Off}
	})
	c := cfg()
	c.AutoPowerOn = true
	kinds := func() map[Kind]int {
		k := map[Kind]int{}
		for _, a := range Decide(in, c) {
			k[a.Kind]++
		}
		return k
	}
	in.Cells[0].UnschedulableFor = 10 * time.Second // the scheduler may still find room
	if k := kinds(); k[PowerOn] != 0 || k[Alert] != 0 {
		t.Fatalf("acted on a pod unschedulable for 10 s: %v", k)
	}
	in.Cells[0].UnschedulableFor = time.Minute
	if k := kinds(); k[PowerOn] != 1 || k[Alert] != 1 {
		t.Fatalf("a cell with nowhere to go for a minute: %v, want the machine powered on and an alert", k)
	}
	c.AutoPowerOn = false
	if k := kinds(); k[PowerOn] != 0 || k[Alert] != 1 {
		t.Fatalf("without auto power-on a person must be told: %v", k)
	}
}

func TestAMachineFoundOffIsFencedBeforeItsLeaseExpires(t *testing.T) {
	in := world()
	in.Cells[0].LeaseUnchanged = 4 * time.Second // three renewals missed; not expired (15 s)
	node(in, "netci-lab-1", func(n *NodeView) { n.MachineState = fence.Off })
	a := only(t, Decide(in, cfg()), FenceNode)
	if a.PowerOff {
		t.Fatal("a machine that is off is not powered off again")
	}
	// Running, or unknown: nothing until the Lease expires.
	for _, st := range []fence.State{fence.Running, fence.Unknown} {
		node(in, "netci-lab-1", func(n *NodeView) { n.MachineState = st; n.KubeletFresh = false })
		none(t, Decide(in, cfg()))
	}
	// Renewed recently: not even a suspect, whatever the power controller says.
	in.Cells[0].LeaseUnchanged = time.Second
	node(in, "netci-lab-1", func(n *NodeView) { n.MachineState = fence.Off })
	none(t, Decide(in, cfg()))
}

// A gap in the observations reset LeaseUnchanged; LeaseQuiet still asks for the power state and
// fences a machine that is off -- and only that.
func TestAQuietLeaseFencesOnlyAMachineThatIsOff(t *testing.T) {
	in := world()
	in.Cells[0].LeaseUnchanged, in.Cells[0].LeaseQuiet = time.Second, 6*time.Second
	node(in, "netci-lab-1", func(n *NodeView) { n.MachineState = fence.Off })
	only(t, Decide(in, cfg()), FenceNode)
	for _, state := range []fence.State{fence.Running, fence.Unknown} {
		node(in, "netci-lab-1", func(n *NodeView) { n.MachineState = state })
		none(t, Decide(in, cfg()))
	}
}
