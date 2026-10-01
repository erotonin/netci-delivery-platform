package fence

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"fmt"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"
)

// powerAgent is an in-process SSH server that behaves like lab/fence/netci-fence: it answers
// `state|off|on <machine>` for the machines it knows and records what it was asked.
type powerAgent struct {
	addr    string
	hostKey ssh.PublicKey
	mu      sync.Mutex
	states  map[string]string
	asked   []string
}

func startPowerAgent(t *testing.T, clientKey ssh.PublicKey) *powerAgent {
	t.Helper()
	_, hostPriv, _ := ed25519.GenerateKey(rand.Reader)
	hostSigner, err := ssh.NewSignerFromKey(hostPriv)
	if err != nil {
		t.Fatal(err)
	}
	cfg := &ssh.ServerConfig{
		PublicKeyCallback: func(_ ssh.ConnMetadata, key ssh.PublicKey) (*ssh.Permissions, error) {
			if string(key.Marshal()) == string(clientKey.Marshal()) {
				return nil, nil
			}
			return nil, fmt.Errorf("unknown key")
		},
	}
	cfg.AddHostKey(hostSigner)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	a := &powerAgent{addr: ln.Addr().String(), hostKey: hostSigner.PublicKey(), states: map[string]string{"netci-lab-1": "running"}}
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go a.serve(conn, cfg)
		}
	}()
	return a
}

func (a *powerAgent) serve(conn net.Conn, cfg *ssh.ServerConfig) {
	_, chans, reqs, err := ssh.NewServerConn(conn, cfg)
	if err != nil {
		return
	}
	go ssh.DiscardRequests(reqs)
	for ch := range chans {
		channel, requests, _ := ch.Accept()
		go func() {
			defer channel.Close()
			for req := range requests {
				if req.Type != "exec" {
					req.Reply(false, nil)
					continue
				}
				req.Reply(true, nil)
				cmd := string(req.Payload[4:])
				fields := strings.Fields(cmd)
				a.mu.Lock()
				a.asked = append(a.asked, cmd)
				status := uint32(0)
				switch {
				case len(fields) == 2 && fields[0] == "state":
					if s, ok := a.states[fields[1]]; ok {
						fmt.Fprintln(channel, s)
					} else {
						status = 3
					}
				case len(fields) == 2 && fields[0] == "off":
					a.states[fields[1]] = "off"
				case len(fields) == 2 && fields[0] == "on":
					a.states[fields[1]] = "running"
				default:
					status = 2
				}
				a.mu.Unlock()
				channel.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{status}))
				return
			}
		}()
	}
}

func client(t *testing.T) (*SSH, ssh.PublicKey) {
	t.Helper()
	_, priv, _ := ed25519.GenerateKey(rand.Reader)
	signer, err := ssh.NewSignerFromKey(priv)
	if err != nil {
		t.Fatal(err)
	}
	return &SSH{User: "deployer", Signer: signer, Timeout: 3 * time.Second}, signer.PublicKey()
}

func TestSSHFencerReadsAndSwitchesPower(t *testing.T) {
	f, key := client(t)
	agent := startPowerAgent(t, key)
	f.Addr, f.HostKey = agent.addr, agent.hostKey
	ctx := context.Background()

	if s, err := f.State(ctx, "netci-lab-1"); err != nil || s != Running {
		t.Fatalf("state = %v, %v", s, err)
	}
	if powered, err := EnsureOff(ctx, f, "netci-lab-1", 3*time.Second); err != nil || !powered {
		t.Fatalf("EnsureOff = %v, %v", powered, err)
	}
	if s, _ := f.State(ctx, "netci-lab-1"); s != Off {
		t.Fatalf("after power-off: %v", s)
	}
}

func TestSSHFencerRefusesAHostKeyItDidNotPin(t *testing.T) {
	f, key := client(t)
	agent := startPowerAgent(t, key)
	_, other, _ := ed25519.GenerateKey(rand.Reader)
	otherSigner, _ := ssh.NewSignerFromKey(other)
	f.Addr, f.HostKey = agent.addr, otherSigner.PublicKey()
	if _, err := f.State(context.Background(), "netci-lab-1"); err == nil {
		t.Fatal("a host that is not the pinned one must be refused")
	}
	if len(agent.asked) != 0 {
		t.Fatalf("nothing should have been sent: %v", agent.asked)
	}
}

func TestSSHFencerRefusesMachineNamesThatCouldCarryArguments(t *testing.T) {
	f, key := client(t)
	agent := startPowerAgent(t, key)
	f.Addr, f.HostKey = agent.addr, agent.hostKey
	for _, name := range []string{"netci-lab-1 netci-lab-2", "x;reboot", "", "-h"} {
		if _, err := f.State(context.Background(), name); err == nil {
			t.Fatalf("%q was accepted", name)
		}
	}
	if len(agent.asked) != 0 {
		t.Fatalf("nothing should have been sent: %v", agent.asked)
	}
}

func TestSSHFencerTreatsAnUnknownMachineAsUnknownNotOff(t *testing.T) {
	f, key := client(t)
	agent := startPowerAgent(t, key)
	f.Addr, f.HostKey = agent.addr, agent.hostKey
	s, err := f.State(context.Background(), "netci-lab-9")
	if err == nil || s != Unknown {
		t.Fatalf("state = %v, %v", s, err)
	}
}
