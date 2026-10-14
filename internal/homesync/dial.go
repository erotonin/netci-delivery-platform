package homesync

import (
	"context"
	"net"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/sys/unix"
)

// DeadPeerAfter is how long a connection to PostgreSQL may go unanswered before it is dropped.
//
// When the machine running the primary loses power, nothing sends a reset: a connection to it
// stays open, a query on it waits for the plugin's 10 s timeout, and the LISTEN connection waits
// for good -- pgx keeps the kernel's defaults (keepalives after minutes, retransmissions for ~15
// minutes). Every pooled connection to the dead primary then costs one request its 10 s while the
// promoted standby is already up. Unanswered data or keepalives drop the connection after this.
const DeadPeerAfter = 10 * time.Second

// PoolConfig parses dsn and dials so that a dead peer is noticed in DeadPeerAfter.
func PoolConfig(dsn string) (*pgxpool.Config, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, err
	}
	d := &net.Dialer{
		Timeout:         5 * time.Second,
		KeepAliveConfig: net.KeepAliveConfig{Enable: true, Idle: 3 * time.Second, Interval: 2 * time.Second, Count: 3},
		Control:         userTimeout(DeadPeerAfter),
	}
	cfg.ConnConfig.DialFunc = func(ctx context.Context, network, addr string) (net.Conn, error) {
		return d.DialContext(ctx, network, addr)
	}
	return cfg, nil
}

// userTimeout sets TCP_USER_TIMEOUT: keepalives notice an idle dead peer, but a connection with
// a query in flight retransmits instead of probing, so it needs its own bound.
func userTimeout(after time.Duration) func(network, address string, c syscall.RawConn) error {
	return func(network, _ string, c syscall.RawConn) error {
		if network != "tcp" && network != "tcp4" && network != "tcp6" {
			return nil // a Unix socket has no peer machine to lose
		}
		var serr error
		if err := c.Control(func(fd uintptr) {
			serr = unix.SetsockoptInt(int(fd), unix.IPPROTO_TCP, unix.TCP_USER_TIMEOUT, int(after.Milliseconds()))
		}); err != nil {
			return err
		}
		return serr
	}
}
