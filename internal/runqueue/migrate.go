// Package runqueue is netCI's durable run queue (ADR-063): every trigger is a row in PostgreSQL
// before it is acknowledged, and a dispatcher hands each run to its cell's controller through
// the netCI plugin's idempotent dispatch until the controller has started it.
package runqueue

import (
	"context"
	"embed"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/erotonin/netci-delivery-platform/internal/pgmigrate"
)

//go:embed migrations/*.sql
var migrations embed.FS

// Migrate applies the queue's migrations.
func Migrate(ctx context.Context, pool *pgxpool.Pool) error {
	return pgmigrate.Apply(ctx, pool, migrations, "migrations")
}
