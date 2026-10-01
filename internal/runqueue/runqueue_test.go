package runqueue

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
)

// These tests need PostgreSQL: NETCI_QUEUE_TEST_DATABASE_URL, a database they may empty.
func testStore(t *testing.T) *Store {
	t.Helper()
	url := os.Getenv("NETCI_QUEUE_TEST_DATABASE_URL")
	if url == "" {
		t.Skip("NETCI_QUEUE_TEST_DATABASE_URL is not set: the queue's PostgreSQL tests did not run")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `TRUNCATE run_events, runs`); err != nil {
		t.Fatal(err)
	}
	return &Store{Pool: pool}
}

// fakeJenkins behaves like the netCI plugin on one controller.
type fakeJenkins struct {
	mu        sync.Mutex
	session   string
	queue     map[string]int64
	builds    map[string]*fakeBuild
	scheduled map[string]int // times each run was put into a queue
	next      int64
	down      bool
	refuse    map[string]bool // jobs the controller refuses
	loseReply bool            // schedule, then fail the call as a timeout would
}

type fakeBuild struct {
	number   int
	building bool
	result   string
}

func newFake() *fakeJenkins {
	return &fakeJenkins{session: uuid.NewString(), queue: map[string]int64{}, builds: map[string]*fakeBuild{},
		scheduled: map[string]int{}, refuse: map[string]bool{}}
}

func (f *fakeJenkins) status(id string) (*Status, bool) {
	if q, ok := f.queue[id]; ok {
		return &Status{State: "queued", Session: f.session, QueueID: q}, true
	}
	if b, ok := f.builds[id]; ok {
		st := &Status{State: "started", Session: f.session}
		st.Build = &struct {
			Number   int    `json:"number"`
			Building bool   `json:"building"`
			Result   string `json:"result"`
			URL      string `json:"url"`
		}{Number: b.number, Building: b.building, Result: b.result, URL: fmt.Sprintf("job/x/%d/", b.number)}
		return st, true
	}
	return &Status{State: "absent", Session: f.session}, false
}

func (f *fakeJenkins) Dispatch(_ context.Context, r *Run, _ string) (*Status, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.down {
		return nil, errors.New("connection refused")
	}
	if f.refuse[r.Job] {
		return nil, &PermanentError{Status: 400, Message: "the job does not declare parameter X"}
	}
	id := r.ID.String()
	if st, ok := f.status(id); ok {
		return st, nil
	}
	f.next++
	f.queue[id] = f.next
	f.scheduled[id]++
	st, _ := f.status(id)
	st.Created = true
	if f.loseReply {
		f.loseReply = false
		return nil, context.DeadlineExceeded
	}
	return st, nil
}

func (f *fakeJenkins) Lookup(_ context.Context, r *Run) (*Status, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.down {
		return nil, false, errors.New("connection refused")
	}
	st, ok := f.status(r.ID.String())
	return st, ok, nil
}

func (f *fakeJenkins) start(id string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.queue, id)
	f.builds[id] = &fakeBuild{number: len(f.builds) + 1, building: true}
}

func (f *fakeJenkins) finish(id, result string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.builds[id].building, f.builds[id].result = false, result
}

// crash: a new JVM; the in-memory queue is gone (JENKINS-30909), build records are kept.
func (f *fakeJenkins) crash() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.session, f.queue = uuid.NewString(), map[string]int64{}
}

// restart: a graceful restart writes queue.xml and reads it back.
func (f *fakeJenkins) restart() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.session = uuid.NewString()
}

func (f *fakeJenkins) cancel(id string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.queue, id)
}

func (f *fakeJenkins) times(id string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.scheduled[id]
}

func dispatcher(s *Store, f Controller, budget int, reg prometheus.Registerer) *Dispatcher {
	return &Dispatcher{Store: s, Cells: []Cell{{Name: "cell-b", Controller: f, Budget: budget}}, Batch: 100,
		Lease: 30 * time.Second, Poll: 0, Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Metrics: NewMetrics(reg)}
}

func accept(t *testing.T, s *Store, job string) *Run {
	t.Helper()
	r, created, err := s.Accept(context.Background(), NewRun{Client: "gitlab", Cell: "cell-b", Job: job, Parameters: map[string]string{"BRANCH": "main"}})
	if err != nil || !created {
		t.Fatalf("accept: %v %v", created, err)
	}
	return r
}

func state(t *testing.T, s *Store, r *Run) *Run {
	t.Helper()
	got, err := s.Get(context.Background(), "gitlab", r.ID)
	if err != nil {
		t.Fatal(err)
	}
	return got
}

func due(t *testing.T, s *Store) {
	t.Helper()
	if _, err := s.Pool.Exec(context.Background(), `UPDATE runs SET next_attempt_at = now()`); err != nil {
		t.Fatal(err)
	}
}

func history(t *testing.T, s *Store, r *Run) []State {
	t.Helper()
	evs, err := s.Events(context.Background(), r.ID)
	if err != nil {
		t.Fatal(err)
	}
	var out []State
	for _, e := range evs {
		out = append(out, e.To)
	}
	return out
}

func TestAcceptIsIdempotentPerClientAndKey(t *testing.T) {
	s := testStore(t)
	ctx := context.Background()
	n := NewRun{Client: "gitlab", IdempotencyKey: "delivery-1", Cell: "cell-b", Job: "payments/api", Parameters: map[string]string{"SHA": "abc"}}
	first, created, err := s.Accept(ctx, n)
	if err != nil || !created {
		t.Fatal(created, err)
	}
	again, created, err := s.Accept(ctx, n)
	if err != nil || created || again.ID != first.ID {
		t.Fatalf("a redelivered webhook made another run: %v %v", created, err)
	}
	n.Parameters = map[string]string{"SHA": "def"}
	if _, _, err := s.Accept(ctx, n); !errors.Is(err, ErrIdempotencyMismatch) {
		t.Fatalf("a reused key for a different run: %v", err)
	}
	other := NewRun{Client: "github", IdempotencyKey: "delivery-1", Cell: "cell-b", Job: "payments/api"}
	if _, created, err := s.Accept(ctx, other); err != nil || !created {
		t.Fatal("keys are per client", err)
	}
	if _, err := s.Get(ctx, "github", first.ID); !errors.Is(err, ErrNotFound) {
		t.Fatal("a client read another client's run")
	}
}

func TestConcurrentAcceptsOfOneKeyMakeOneRun(t *testing.T) {
	s := testStore(t)
	var wg sync.WaitGroup
	ids := make(chan uuid.UUID, 20)
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r, _, err := s.Accept(context.Background(), NewRun{Client: "gitlab", IdempotencyKey: "same", Cell: "cell-b", Job: "j"})
			if err != nil {
				t.Error(err)
				return
			}
			ids <- r.ID
		}()
	}
	wg.Wait()
	close(ids)
	seen := map[uuid.UUID]bool{}
	for id := range ids {
		seen[id] = true
	}
	if len(seen) != 1 {
		t.Fatalf("%d runs for one key", len(seen))
	}
}

func TestOnlyTheTransitionsOfTheStateMachineHappen(t *testing.T) {
	s := testStore(t)
	ctx := context.Background()
	r := accept(t, s, "j")
	if err := s.Transition(ctx, r.ID, Accepted, Update{To: Finished}); err == nil {
		t.Fatal("accepted -> finished: a run reported finished without having started")
	}
	if err := s.Transition(ctx, r.ID, Dispatched, Update{To: Started, BuildNumber: 1}); !errors.Is(err, ErrConflict) {
		t.Fatalf("a transition from a state the run is not in: %v", err)
	}
	if err := s.Transition(ctx, r.ID, Accepted, Update{To: Started}); err == nil {
		t.Fatal("started without a build number")
	}
}

func TestARunGoesFromAcceptedToFinishedThroughItsController(t *testing.T) {
	s, f := testStore(t), newFake()
	d := dispatcher(s, f, 10, prometheus.NewRegistry())
	ctx := context.Background()
	r := accept(t, s, "payments/api")
	d.Tick(ctx)
	if got := state(t, s, r); got.State != Dispatched || got.Session == "" || got.QueueID == 0 {
		t.Fatalf("%+v", got)
	}
	f.start(r.ID.String())
	d.Tick(ctx)
	if got := state(t, s, r); got.State != Started || got.BuildNumber != 1 {
		t.Fatalf("%+v", got)
	}
	f.finish(r.ID.String(), "SUCCESS")
	d.Tick(ctx)
	got := state(t, s, r)
	if got.State != Finished || got.Result != "SUCCESS" || got.FinishedAt == nil {
		t.Fatalf("%+v", got)
	}
	if h := history(t, s, r); fmt.Sprint(h) != "[accepted dispatched started finished]" {
		t.Fatalf("history %v", h)
	}
}

func TestARunLostWithACrashedControllersQueueIsDispatchedAgainAndRunsOnce(t *testing.T) {
	s, f := testStore(t), newFake()
	reg := prometheus.NewRegistry()
	d := dispatcher(s, f, 10, reg)
	ctx := context.Background()
	r := accept(t, s, "payments/api")
	d.Tick(ctx)
	f.crash()
	d.Tick(ctx)
	got := state(t, s, r)
	if got.State != Dispatched || got.Session != f.session {
		t.Fatalf("not dispatched to the new controller: %+v", got)
	}
	if f.times(r.ID.String()) != 2 {
		t.Fatalf("scheduled %d times: once before the crash, once after", f.times(r.ID.String()))
	}
	for i := 0; i < 5; i++ {
		d.Tick(ctx)
	}
	if f.times(r.ID.String()) != 2 {
		t.Fatal("dispatched again into a controller that already had it")
	}
	f.start(r.ID.String())
	f.finish(r.ID.String(), "SUCCESS")
	d.Tick(ctx)
	if got := state(t, s, r); got.State != Finished || len(f.builds) != 1 {
		t.Fatalf("%+v, %d builds", got, len(f.builds))
	}
}

func TestAGracefulRestartKeepsTheQueueAndNothingIsDuplicated(t *testing.T) {
	s, f := testStore(t), newFake()
	d := dispatcher(s, f, 10, prometheus.NewRegistry())
	ctx := context.Background()
	r := accept(t, s, "payments/api")
	d.Tick(ctx)
	f.restart()
	d.Tick(ctx)
	got := state(t, s, r)
	if got.State != Dispatched || got.Session != f.session || f.times(r.ID.String()) != 1 {
		t.Fatalf("%+v scheduled %d", got, f.times(r.ID.String()))
	}
}

func TestARunCancelledInJenkinsIsNotOverruled(t *testing.T) {
	s, f := testStore(t), newFake()
	d := dispatcher(s, f, 10, prometheus.NewRegistry())
	ctx := context.Background()
	r := accept(t, s, "payments/api")
	d.Tick(ctx)
	f.cancel(r.ID.String())
	d.Tick(ctx)
	d.Tick(ctx)
	if got := state(t, s, r); got.State != Cancelled || f.times(r.ID.String()) != 1 {
		t.Fatalf("%+v scheduled %d", got, f.times(r.ID.String()))
	}
}

func TestRefusalsAreFinalAndOutagesAreRetried(t *testing.T) {
	s, f := testStore(t), newFake()
	d := dispatcher(s, f, 10, prometheus.NewRegistry())
	ctx := context.Background()
	f.refuse["broken"] = true
	bad := accept(t, s, "broken")
	f.down = true
	ok := accept(t, s, "payments/api")
	d.Tick(ctx)
	if got := state(t, s, ok); got.State != Accepted || got.Attempts != 1 || got.LastError == "" {
		t.Fatalf("an outage must leave the run accepted with the error: %+v", got)
	}
	d.Tick(ctx) // not due yet: the backoff holds it
	if got := state(t, s, ok); got.Attempts != 1 {
		t.Fatalf("retried before its backoff: %+v", got)
	}
	f.down = false
	due(t, s)
	d.Tick(ctx)
	if got := state(t, s, ok); got.State != Dispatched || got.Attempts != 0 || got.LastError != "" {
		t.Fatalf("%+v", got)
	}
	got := state(t, s, bad)
	if got.State != Refused || got.FinishedAt == nil {
		t.Fatalf("%+v", got)
	}
	evs, _ := s.Events(ctx, bad.ID)
	if last := evs[len(evs)-1]; last.Detail["message"] != "the job does not declare parameter X" {
		t.Fatalf("the refusal's reason is not recorded: %+v", last)
	}
}

func TestALostReplyIsNotADuplicate(t *testing.T) {
	s, f := testStore(t), newFake()
	d := dispatcher(s, f, 10, prometheus.NewRegistry())
	ctx := context.Background()
	f.loseReply = true
	r := accept(t, s, "payments/api")
	d.Tick(ctx)
	if got := state(t, s, r); got.State != Accepted {
		t.Fatalf("%+v", got)
	}
	due(t, s)
	d.Tick(ctx)
	if got := state(t, s, r); got.State != Dispatched || f.times(r.ID.String()) != 1 {
		t.Fatalf("%+v scheduled %d", got, f.times(r.ID.String()))
	}
}

func TestTheBudgetBoundsWhatSitsInAControllersQueue(t *testing.T) {
	s, f := testStore(t), newFake()
	d := dispatcher(s, f, 2, prometheus.NewRegistry())
	ctx := context.Background()
	var runs []*Run
	for i := 0; i < 5; i++ {
		runs = append(runs, accept(t, s, "payments/api"))
	}
	d.Tick(ctx)
	if n, _ := s.Outstanding(ctx, "cell-b"); n != 2 {
		t.Fatalf("%d in the controller's queue", n)
	}
	// Oldest first.
	if state(t, s, runs[0]).State != Dispatched || state(t, s, runs[4]).State != Accepted {
		t.Fatal("not dispatched oldest first")
	}
	f.start(runs[0].ID.String())
	d.Tick(ctx)
	if n, _ := s.Outstanding(ctx, "cell-b"); n != 2 {
		t.Fatalf("%d in the controller's queue after one started", n)
	}
}

func TestTwoDispatchersRacingScheduleEveryRunOnce(t *testing.T) {
	s, f := testStore(t), newFake()
	reg := prometheus.NewRegistry()
	d1 := dispatcher(s, f, 1000, reg)
	d2 := dispatcher(s, f, 1000, prometheus.NewRegistry())
	d1.Lease, d2.Lease = 0, 0 // claims overlap as much as they can
	var runs []*Run
	for i := 0; i < 60; i++ {
		runs = append(runs, accept(t, s, "payments/api"))
	}
	var wg sync.WaitGroup
	for _, d := range []*Dispatcher{d1, d2} {
		wg.Add(1)
		go func(d *Dispatcher) {
			defer wg.Done()
			for i := 0; i < 10; i++ {
				d.Tick(context.Background())
			}
		}(d)
	}
	wg.Wait()
	for _, r := range runs {
		if n := f.times(r.ID.String()); n != 1 {
			t.Fatalf("run %s scheduled %d times", r.ID, n)
		}
		if got := state(t, s, r); got.State != Dispatched {
			t.Fatalf("%+v", got)
		}
	}
}
