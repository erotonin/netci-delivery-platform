package homesync

import (
	"context"
	"net"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// The dial a pool uses really sets the bounds on the socket: read back from the kernel, not from
// the config, because a Control func that is never called (or fails silently) leaves the
// kernel's 15-minute default and nothing else would show it.
func TestPoolConfigDialsWithDeadPeerBounds(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		if c, err := ln.Accept(); err == nil {
			defer c.Close()
			time.Sleep(time.Second)
		}
	}()
	cfg, err := PoolConfig("postgresql://u:p@" + ln.Addr().String() + "/db")
	if err != nil {
		t.Fatal(err)
	}
	conn, err := cfg.ConnConfig.DialFunc(context.Background(), "tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	raw, err := conn.(*net.TCPConn).SyscallConn()
	if err != nil {
		t.Fatal(err)
	}
	var userTimeout, keepalive, idle int
	var gerr error
	if err := raw.Control(func(fd uintptr) {
		if userTimeout, gerr = unix.GetsockoptInt(int(fd), unix.IPPROTO_TCP, unix.TCP_USER_TIMEOUT); gerr != nil {
			return
		}
		if keepalive, gerr = unix.GetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_KEEPALIVE); gerr != nil {
			return
		}
		idle, gerr = unix.GetsockoptInt(int(fd), unix.IPPROTO_TCP, unix.TCP_KEEPIDLE)
	}); err != nil || gerr != nil {
		t.Fatal(err, gerr)
	}
	if userTimeout != int(DeadPeerAfter.Milliseconds()) {
		t.Errorf("TCP_USER_TIMEOUT = %d ms, want %d", userTimeout, DeadPeerAfter.Milliseconds())
	}
	if keepalive == 0 || idle != 3 {
		t.Errorf("keepalive = %d, idle = %d s; want on, 3 s", keepalive, idle)
	}
}
