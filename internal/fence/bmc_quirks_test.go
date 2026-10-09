package fence

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// quirkyBMC behaves as real BMCs are documented to, where the sushy-tools emulator the lab uses
// answers at once and always succeeds:
//   - iDRAC answers ForceOff on a machine already off with 409 "Server is already powered OFF"
//     (Red Hat bug 1873305), and refuses with 409 for a while after a power change
//     (openshift/openstack-ironic#502);
//   - a BMC's PowerState lags the power: it says On for seconds after accepting ForceOff;
//   - a slow BMC carries out a request whose answer comes after the client gave up;
//   - a busy one answers 503;
//   - OpenBMC names its allowed reset types through @Redfish.ActionInfo, not inline.
type quirkyBMC struct {
	mu         sync.Mutex
	on         bool          // the machine's real power
	reported   string        // what PowerState says; lags `on` by lag
	offAt      time.Time     // when the machine really went off
	lag        time.Duration // PowerState keeps saying On this long after the power went
	postDelay  time.Duration // a POST is carried out at once but answered this late
	busy       int           // the next n POSTs answer 503, doing nothing
	settling   int           // the next n POSTs answer 409 "not settled", doing nothing
	refuse     int           // HTTP status every POST answers with, doing nothing (0: none)
	actionInfo bool          // OpenBMC style: no inline AllowableValues
	posts      int
}

func (b *quirkyBMC) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if u, p, ok := r.BasicAuth(); !ok || u != user || p != pass {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	b.mu.Lock()
	switch {
	case r.Method == http.MethodGet && r.URL.Path == sys:
		state := "On"
		if !b.on && time.Since(b.offAt) >= b.lag {
			state = "Off"
		}
		reset := map[string]any{"target": sys + "/Actions/ComputerSystem.Reset"}
		if b.actionInfo {
			reset["@Redfish.ActionInfo"] = sys + "/ResetActionInfo"
		} else {
			reset["ResetType@Redfish.AllowableValues"] = []string{"On", "ForceOff", "GracefulShutdown", "ForceRestart"}
		}
		b.mu.Unlock()
		_ = json.NewEncoder(w).Encode(map[string]any{"PowerState": state, "Actions": map[string]any{"#ComputerSystem.Reset": reset}})
	case r.Method == http.MethodPost && r.URL.Path == sys+"/Actions/ComputerSystem.Reset":
		b.posts++
		delay := b.postDelay
		status := http.StatusNoContent
		switch {
		case b.refuse != 0:
			status = b.refuse
		case b.busy > 0:
			b.busy--
			status = http.StatusServiceUnavailable
		case b.settling > 0:
			b.settling--
			status = http.StatusConflict
		case !b.on:
			status = http.StatusConflict // "already powered OFF"
		default:
			b.on, b.offAt = false, time.Now()
		}
		b.mu.Unlock()
		time.Sleep(delay)
		w.WriteHeader(status)
		if status == http.StatusConflict {
			_, _ = w.Write([]byte(`{"error":{"code":"Base.1.12.GeneralError","message":"","@Message.ExtendedInfo":[{"Message":"Server is already powered OFF.","MessageId":"IDRAC.2.9.PSU501"}]}}`))
		}
	default:
		b.mu.Unlock()
		w.WriteHeader(http.StatusNotFound)
	}
}

func (b *quirkyBMC) powerLoss() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.on, b.offAt = false, time.Now()
}

func (b *quirkyBMC) postsSent() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.posts
}

func quirky(t *testing.T, b *quirkyBMC, requestTimeout time.Duration) *Redfish {
	t.Helper()
	srv := httptest.NewTLSServer(b)
	t.Cleanup(srv.Close)
	sum := sha256.Sum256(srv.Certificate().Raw)
	r, err := NewRedfish(srv.URL, user, pass, RedfishTLS{LeafSHA256: hex.EncodeToString(sum[:]), RequestTimeout: requestTimeout})
	if err != nil {
		t.Fatal(err)
	}
	return r
}

// The machine lost its power between the state read and the ForceOff: iDRAC answers 409. The
// machine is off; fencing must say so, not fail.
func TestAMachineThatWentOffBeforeTheForceOffIsFenced(t *testing.T) {
	b := &quirkyBMC{on: true}
	r := quirky(t, b, time.Second)
	f := &onRead{Redfish: r, then: b.powerLoss}
	if _, err := EnsureOff(context.Background(), f, sys, 5*time.Second); err != nil {
		t.Fatalf("an iDRAC 409 on a machine that is off failed the fencing: %v", err)
	}
}

// onRead runs `then` after the first State answers: the power goes in between.
type onRead struct {
	*Redfish
	then func()
	once sync.Once
}

func (o *onRead) State(ctx context.Context, m string) (State, error) {
	s, err := o.Redfish.State(ctx, m)
	o.once.Do(o.then)
	return s, err
}

func TestAPowerStateThatLagsIsWaitedFor(t *testing.T) {
	b := &quirkyBMC{on: true, lag: 2 * time.Second}
	start := time.Now()
	powered, err := EnsureOff(context.Background(), quirky(t, b, time.Second), sys, 5*time.Second)
	if err != nil || !powered {
		t.Fatalf("%v %v", powered, err)
	}
	if time.Since(start) < 2*time.Second {
		t.Fatal("confirmed off while the BMC still said On")
	}
}

func TestAForceOffAnsweredAfterTheClientGaveUpStillCounts(t *testing.T) {
	b := &quirkyBMC{on: true, postDelay: 1500 * time.Millisecond}
	if _, err := EnsureOff(context.Background(), quirky(t, b, 500*time.Millisecond), sys, 5*time.Second); err != nil {
		t.Fatalf("the BMC powered the machine off but its late answer failed the fencing: %v", err)
	}
}

func TestABusyOrSettlingBMCIsAskedAgain(t *testing.T) {
	for name, b := range map[string]*quirkyBMC{
		"503 twice":            {on: true, busy: 2},
		"409 while it settles": {on: true, settling: 2},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := EnsureOff(context.Background(), quirky(t, b, time.Second), sys, 10*time.Second); err != nil {
				t.Fatal(err)
			}
			if n := b.postsSent(); n != 3 {
				t.Fatalf("%d ForceOffs, want 3", n)
			}
		})
	}
}

func TestARefusedForceOffFailsAtOnceAndIsNotRepeated(t *testing.T) {
	b := &quirkyBMC{on: true, refuse: http.StatusBadRequest}
	start := time.Now()
	_, err := EnsureOff(context.Background(), quirky(t, b, time.Second), sys, 10*time.Second)
	if err == nil || !strings.Contains(err.Error(), "HTTP 400") {
		t.Fatalf("%v", err)
	}
	if time.Since(start) > time.Second || b.postsSent() != 1 {
		t.Fatalf("a refusal was waited on or repeated: %s, %d posts", time.Since(start), b.postsSent())
	}
}

func TestAMachineThatNeverReportsOffIsNotFenced(t *testing.T) {
	b := &quirkyBMC{on: true, settling: 1 << 30}
	_, err := EnsureOff(context.Background(), quirky(t, b, time.Second), sys, 3*time.Second)
	if err == nil || !strings.Contains(err.Error(), "did not report off") || !strings.Contains(err.Error(), "already powered OFF") {
		t.Fatalf("%v", err)
	}
}

func TestOpenBMCsActionInfoIsAcceptedAndForceOffSent(t *testing.T) {
	b := &quirkyBMC{on: true, actionInfo: true}
	if _, err := EnsureOff(context.Background(), quirky(t, b, time.Second), sys, 5*time.Second); err != nil {
		t.Fatal(err)
	}
}
