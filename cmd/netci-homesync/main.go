// Command netci-homesync keeps the Jenkins replicas' homes the same (ADR-076): the order every
// change to an item is made in, and the feed each replica follows to apply the others' changes.
//
// It holds nothing itself -- PostgreSQL does -- so several run at once behind one Service.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/erotonin/netci-delivery-platform/internal/homesync"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(log); err != nil {
		log.Error("netci-homesync stopped", "error", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		return errors.New("DATABASE_URL is required: the homes' shared order lives in PostgreSQL")
	}
	// Without the replicas' credentials nothing could tell one replica's change from another's,
	// nor from a stranger's: refuse to start rather than accept changes from anyone.
	replicas, err := homesync.LoadReplicas(getenv("NETCI_HOMESYNC_REPLICAS_FILE", "/run/secrets/netci-homesync/replicas"))
	if err != nil {
		return fmt.Errorf("replica credentials: %w", err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return fmt.Errorf("database: %w", err)
	}
	defer pool.Close()
	if err := homesync.Migrate(ctx, pool); err != nil {
		return fmt.Errorf("migrate: %w", err)
	}
	srv := homesync.NewServer(ctx, &homesync.Store{Pool: pool}, replicas, log)
	// WriteTimeout above the longest long poll (30 s), or every idle poll would be cut.
	server := &http.Server{Addr: getenv("NETCI_HTTP_ADDR", ":8080"), Handler: srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 30 * time.Second, WriteTimeout: 45 * time.Second}
	go func() {
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("http server", "error", err)
			stop()
		}
	}()
	log.Info("netci-homesync started")
	<-ctx.Done()
	shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return server.Shutdown(shutdown)
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
