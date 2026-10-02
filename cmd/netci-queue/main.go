// Command netci-queue is netCI's durable run queue (ADR-063): the intake API that accepts runs
// into PostgreSQL, and the dispatcher that hands them to their cell's controller through the
// netCI plugin until each has started.
//
// Several replicas may run at once and all of them dispatch: claims skip each other's rows,
// transitions are compare-and-set, and dispatch is idempotent on the controller.
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
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/erotonin/netci-delivery-platform/internal/runqueue"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(log); err != nil {
		log.Error("netci-queue stopped", "error", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		return errors.New("DATABASE_URL is required: the queue lives in PostgreSQL and nothing is accepted without it")
	}
	cfg, err := runqueue.LoadConfig(getenv("NETCI_QUEUE_CONFIG", "/etc/netci/queue/config.json"))
	if err != nil {
		return err
	}
	cells, err := cfg.Controllers(10 * time.Second)
	if err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return fmt.Errorf("database: %w", err)
	}
	defer pool.Close()
	if err := runqueue.Migrate(ctx, pool); err != nil {
		return fmt.Errorf("migrate: %w", err)
	}

	registry := prometheus.NewRegistry()
	registry.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	metrics := runqueue.NewMetrics(registry)
	cellNames := make([]string, 0, len(cfg.Cells))
	for name := range cfg.Cells {
		cellNames = append(cellNames, name)
	}
	metrics.Known(cellNames)
	store := &runqueue.Store{Pool: pool}
	intake := &runqueue.Intake{Store: store, Config: cfg, Log: log, Metrics: metrics}

	mux := http.NewServeMux()
	mux.Handle("/v1/", intake.Handler())
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		pctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := pool.Ping(pctx); err != nil {
			http.Error(w, "database unreachable: runs cannot be accepted", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	})
	mux.Handle("GET /metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))
	server := &http.Server{Addr: getenv("NETCI_HTTP_ADDR", ":8080"), Handler: mux,
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 30 * time.Second, WriteTimeout: 30 * time.Second}
	go func() {
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("http server", "error", err)
			stop()
		}
	}()

	d := &runqueue.Dispatcher{Store: store, Cells: cells, Batch: 50, Lease: 30 * time.Second, Poll: 2 * time.Second, Log: log, Metrics: metrics}
	log.Info("netci-queue started", "cells", len(cells), "clients", len(cfg.Clients))
	d.Run(ctx, time.Second)

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
