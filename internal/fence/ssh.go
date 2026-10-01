package fence

import (
	"bytes"
	"context"
	"fmt"
	"net"
	"regexp"
	"strings"
	"time"

	"golang.org/x/crypto/ssh"
)

// SSH fences machines through a power agent reached over SSH: the account's key is bound in
// authorized_keys to one forced command (lab/fence/netci-fence for libvirt), which accepts
// only `state|off|on <machine>`. The supervisor holds the power of the machines and nothing
// else on that host.
type SSH struct {
	Addr    string // host:port
	User    string
	Signer  ssh.Signer
	HostKey ssh.PublicKey // pinned; an unknown or changed host key is refused
	Timeout time.Duration
}

var machineName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$`)

func (s *SSH) Name() string { return "ssh:" + s.Addr }

func (s *SSH) State(ctx context.Context, machine string) (State, error) {
	out, err := s.run(ctx, "state", machine)
	if err != nil {
		return Unknown, err
	}
	switch strings.TrimSpace(out) {
	case "running":
		return Running, nil
	case "off":
		return Off, nil
	default:
		return Unknown, fmt.Errorf("power agent answered %q", strings.TrimSpace(out))
	}
}

func (s *SSH) PowerOff(ctx context.Context, machine string) error {
	_, err := s.run(ctx, "off", machine)
	return err
}

func (s *SSH) PowerOn(ctx context.Context, machine string) error {
	_, err := s.run(ctx, "on", machine)
	return err
}

func (s *SSH) run(ctx context.Context, verb, machine string) (string, error) {
	if !machineName.MatchString(machine) {
		return "", fmt.Errorf("refusing machine name %q", machine)
	}
	timeout := s.Timeout
	if timeout == 0 {
		timeout = 10 * time.Second
	}
	cfg := &ssh.ClientConfig{
		User:            s.User,
		Auth:            []ssh.AuthMethod{ssh.PublicKeys(s.Signer)},
		HostKeyCallback: ssh.FixedHostKey(s.HostKey),
		Timeout:         timeout,
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	conn, err := (&net.Dialer{}).DialContext(ctx, "tcp", s.Addr)
	if err != nil {
		return "", err
	}
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}
	c, chans, reqs, err := ssh.NewClientConn(conn, s.Addr, cfg)
	if err != nil {
		conn.Close()
		return "", err
	}
	client := ssh.NewClient(c, chans, reqs)
	defer client.Close()
	session, err := client.NewSession()
	if err != nil {
		return "", err
	}
	defer session.Close()
	var stdout, stderr bytes.Buffer
	session.Stdout, session.Stderr = &stdout, &stderr
	if err := session.Run(verb + " " + machine); err != nil {
		return stdout.String(), fmt.Errorf("%s %s: %w: %s", verb, machine, err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
}
