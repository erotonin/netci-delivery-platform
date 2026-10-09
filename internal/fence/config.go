package fence

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/crypto/ssh"
)

// Config says, for every node the supervisor may fence, how to reach its power controller.
// Secrets are not in it: each entry names a directory (mounted from a Secret) holding them.
//
//	{"nodes": {
//	  "worker-7":    {"redfish": {"endpoint": "https://10.0.8.17", "system": "/redfish/v1/Systems/1",
//	                              "credentials": "/etc/netci/fence/bmc-worker-7"}},
//	  "netci-lab-1": {"ssh": {"addr": "192.168.122.1:22", "user": "deployer", "machine": "netci-lab-1",
//	                          "credentials": "/etc/netci/fence/lab"}}}}
//
// A Redfish credentials directory holds username, password, and either ca.crt or tls-sha256.
// An SSH one holds id_ed25519 and host_key.pub.
type Config struct {
	Nodes map[string]NodeConfig `json:"nodes"`
}

// NodeConfig names exactly one power controller.
type NodeConfig struct {
	Redfish *RedfishConfig `json:"redfish,omitempty"`
	SSH     *SSHConfig     `json:"ssh,omitempty"`
}

type RedfishConfig struct {
	Endpoint    string `json:"endpoint"`
	System      string `json:"system"`
	Credentials string `json:"credentials"`
	ServerName  string `json:"serverName,omitempty"`
	// RequestTimeout bounds each request to the BMC ("5s" by default). A BMC that takes longer
	// to answer reads as Unknown and is never fenced, so set it from the BMC's measured
	// latency.
	RequestTimeout string `json:"requestTimeout,omitempty"`
}

type SSHConfig struct {
	Addr        string `json:"addr"`
	User        string `json:"user"`
	Machine     string `json:"machine"`
	Credentials string `json:"credentials"`
}

// LoadConfig reads the file and builds a Router whose machine names are the node names. Any
// doubt -- an unknown field, a node with no or two controllers, a missing secret -- is an error:
// a supervisor that cannot fence a node must say so at start, not when the node fails.
func LoadConfig(path string) (*Router, map[string]string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var cfg Config
	if err := dec.Decode(&cfg); err != nil {
		return nil, nil, fmt.Errorf("fence config %s: %w", path, err)
	}
	if len(cfg.Nodes) == 0 {
		return nil, nil, fmt.Errorf("fence config %s names no nodes", path)
	}
	targets, machines := map[string]Target{}, map[string]string{}
	bmcs := map[string]*Redfish{} // one client per BMC and credential, shared by its systems
	for node, nc := range cfg.Nodes {
		var t Target
		switch {
		case nc.Redfish != nil && nc.SSH == nil:
			key := nc.Redfish.Endpoint + "|" + nc.Redfish.Credentials
			r, ok := bmcs[key]
			if !ok {
				if r, err = loadRedfish(nc.Redfish); err != nil {
					return nil, nil, fmt.Errorf("node %s: %w", node, err)
				}
				bmcs[key] = r
			}
			if err := checkSystemPath(nc.Redfish.System); err != nil {
				return nil, nil, fmt.Errorf("node %s: %w", node, err)
			}
			t = Target{Fencer: r, ID: nc.Redfish.System}
		case nc.SSH != nil && nc.Redfish == nil:
			s, err := loadSSH(nc.SSH)
			if err != nil {
				return nil, nil, fmt.Errorf("node %s: %w", node, err)
			}
			if !machineName.MatchString(nc.SSH.Machine) {
				return nil, nil, fmt.Errorf("node %s: refusing machine name %q", node, nc.SSH.Machine)
			}
			t = Target{Fencer: s, ID: nc.SSH.Machine}
		default:
			return nil, nil, fmt.Errorf("node %s must name exactly one power controller (redfish or ssh)", node)
		}
		targets[node], machines[node] = t, node
	}
	router, err := NewRouter(targets)
	if err != nil {
		return nil, nil, err
	}
	return router, machines, nil
}

func loadRedfish(c *RedfishConfig) (*Redfish, error) {
	read := func(name string) (string, error) {
		b, err := os.ReadFile(filepath.Join(c.Credentials, name))
		return strings.TrimSpace(string(b)), err
	}
	if c.Credentials == "" {
		return nil, errors.New("redfish needs a credentials directory")
	}
	user, err := read("username")
	if err != nil {
		return nil, fmt.Errorf("redfish credentials: %w", err)
	}
	pass, err := read("password")
	if err != nil {
		return nil, fmt.Errorf("redfish credentials: %w", err)
	}
	pin := RedfishTLS{ServerName: c.ServerName, RequestTimeout: 5 * time.Second}
	if c.RequestTimeout != "" {
		d, err := time.ParseDuration(c.RequestTimeout)
		if err != nil || d <= 0 || d > time.Minute {
			return nil, fmt.Errorf("redfish requestTimeout %q: a duration up to 1m", c.RequestTimeout)
		}
		pin.RequestTimeout = d
	}
	if ca, err := os.ReadFile(filepath.Join(c.Credentials, "ca.crt")); err == nil {
		pin.CAPEM = ca
	}
	if fp, err := read("tls-sha256"); err == nil {
		pin.LeafSHA256 = fp
	}
	return NewRedfish(c.Endpoint, user, pass, pin)
}

func loadSSH(c *SSHConfig) (*SSH, error) {
	if c.Addr == "" || c.User == "" || c.Credentials == "" {
		return nil, errors.New("ssh needs addr, user and a credentials directory")
	}
	keyPEM, err := os.ReadFile(filepath.Join(c.Credentials, "id_ed25519"))
	if err != nil {
		return nil, fmt.Errorf("ssh key: %w", err)
	}
	signer, err := ssh.ParsePrivateKey(keyPEM)
	if err != nil {
		return nil, errors.New("ssh key: not a usable private key") // never echo key material
	}
	hk, err := os.ReadFile(filepath.Join(c.Credentials, "host_key.pub"))
	if err != nil {
		return nil, fmt.Errorf("ssh host key: %w", err)
	}
	hostKey, _, _, _, err := ssh.ParseAuthorizedKey(hk)
	if err != nil {
		return nil, fmt.Errorf("ssh host key: want one public key line: %w", err)
	}
	return &SSH{Addr: c.Addr, User: c.User, Signer: signer, HostKey: hostKey, Timeout: 3 * time.Second}, nil
}
