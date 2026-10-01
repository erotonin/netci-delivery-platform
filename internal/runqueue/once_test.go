package runqueue

import (
	"context"
	"net/http"
	"sync"
	"testing"

	"github.com/google/uuid"
)

func TestABlockRecordsItselfOnceAndASecondAttemptIsRefused(t *testing.T) {
	s := testStore(t)
	ctx := context.Background()
	first, err := s.Once(ctx, "cell-b", "jenkins-x/payments/deploy#42", "deploy-prod", "nonce-attempt-one")
	if err != nil || !first.First {
		t.Fatalf("%+v %v", first, err)
	}
	retry, _ := s.Once(ctx, "cell-b", "jenkins-x/payments/deploy#42", "deploy-prod", "nonce-attempt-one")
	if !retry.First {
		t.Fatal("a retried call of the same attempt was refused")
	}
	again, _ := s.Once(ctx, "cell-b", "jenkins-x/payments/deploy#42", "deploy-prod", "nonce-attempt-two")
	if again.First || !again.RecordedAt.Equal(first.RecordedAt) {
		t.Fatalf("a re-execution after a rollback was allowed: %+v", again)
	}
	other, _ := s.Once(ctx, "cell-b", "jenkins-x/payments/deploy#43", "deploy-prod", "nonce-attempt-two")
	if !other.First {
		t.Fatal("another build's block was refused")
	}
	otherCell, _ := s.Once(ctx, "cell-c", "jenkins-x/payments/deploy#42", "deploy-prod", "nonce-attempt-two")
	if !otherCell.First {
		t.Fatal("markers are per client")
	}
}

func TestConcurrentAttemptsOfOneBlockLetExactlyOneRun(t *testing.T) {
	s := testStore(t)
	var wg sync.WaitGroup
	var mu sync.Mutex
	firsts, errs := 0, 0
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r, err := s.Once(context.Background(), "cell-b", "scope", "deploy", uuid.NewString())
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				errs++
			} else if r.First {
				firsts++
			}
		}()
	}
	wg.Wait()
	if firsts != 1 || errs != 0 {
		t.Fatalf("%d attempts allowed to run, %d errors", firsts, errs)
	}
}

func TestTheOnceEndpoint(t *testing.T) {
	a, _ := newAPI(t)
	body := `{"scope":"jenkins-x/payments/deploy#42","key":"deploy-prod","nonce":"0123456789abcdef-1"}`
	if code, r := a.do("POST", "/v1/once", "gitlab-token", body); code != http.StatusOK || r["first"] != true {
		t.Fatalf("%d %v", code, r)
	}
	if code, _ := a.do("POST", "/v1/once", "gitlab-token", body); code != http.StatusOK {
		t.Fatalf("a retry of the same attempt: %d", code)
	}
	if code, r := a.do("POST", "/v1/once", "gitlab-token", `{"scope":"jenkins-x/payments/deploy#42","key":"deploy-prod","nonce":"0123456789abcdef-2"}`); code != http.StatusConflict || r["first"] != false {
		t.Fatalf("a re-execution: %d %v", code, r)
	}
	if code, _ := a.do("POST", "/v1/once", "", body); code != http.StatusUnauthorized {
		t.Fatal(code)
	}
	for _, bad := range []string{`{"scope":"","key":"k","nonce":"0123456789abcdef"}`, `{"scope":"s","key":"","nonce":"0123456789abcdef"}`,
		`{"scope":"s","key":"k","nonce":"short"}`, `not json`} {
		if code, _ := a.do("POST", "/v1/once", "gitlab-token", bad); code != http.StatusBadRequest {
			t.Errorf("%s: %d", bad, code)
		}
	}
}
