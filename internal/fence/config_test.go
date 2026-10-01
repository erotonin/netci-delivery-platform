package fence

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/pem"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/crypto/ssh"
)

func writeFiles(t *testing.T, dir string, files map[string]string) string {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func sshCredentials(t *testing.T, dir string) string {
	_, priv, _ := ed25519.GenerateKey(rand.Reader)
	block, err := ssh.MarshalPrivateKey(priv, "")
	if err != nil {
		t.Fatal(err)
	}
	hostPub, _, _ := ed25519.GenerateKey(rand.Reader)
	hk, _ := ssh.NewPublicKey(hostPub)
	return writeFiles(t, dir, map[string]string{"id_ed25519": string(pem.EncodeToMemory(block)), "host_key.pub": string(ssh.MarshalAuthorizedKey(hk))})
}

func TestLoadConfigBuildsOneRouterForMixedPowerControllers(t *testing.T) {
	root := t.TempDir()
	bmc := writeFiles(t, filepath.Join(root, "bmc"), map[string]string{"username": "fence\n", "password": "pw\n", "tls-sha256": strings.Repeat("ab", 32)})
	lab := sshCredentials(t, filepath.Join(root, "lab"))
	cfg := writeFiles(t, root, map[string]string{"fence.json": `{"nodes": {
	  "bm-1": {"redfish": {"endpoint": "https://10.0.8.17", "system": "/redfish/v1/Systems/1", "credentials": "` + bmc + `"}},
	  "bm-2": {"redfish": {"endpoint": "https://10.0.8.17", "system": "/redfish/v1/Systems/2", "credentials": "` + bmc + `"}},
	  "vm-1": {"ssh": {"addr": "192.168.122.1:22", "user": "deployer", "machine": "netci-lab-1", "credentials": "` + lab + `"}}}}`})
	router, machines, err := LoadConfig(filepath.Join(cfg, "fence.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(machines) != 3 || machines["bm-1"] != "bm-1" {
		t.Fatalf("%v", machines)
	}
	if router.Targets["bm-1"].Fencer != router.Targets["bm-2"].Fencer {
		t.Fatal("two systems behind one BMC should share its client")
	}
	if router.Targets["vm-1"].ID != "netci-lab-1" || router.Targets["bm-2"].ID != "/redfish/v1/Systems/2" {
		t.Fatalf("%+v", router.Targets)
	}
}

func TestLoadConfigRefusesAnythingDoubtful(t *testing.T) {
	root := t.TempDir()
	bmc := writeFiles(t, filepath.Join(root, "bmc"), map[string]string{"username": "fence", "password": "pw", "tls-sha256": strings.Repeat("ab", 32)})
	nopass := writeFiles(t, filepath.Join(root, "nopass"), map[string]string{"username": "fence", "tls-sha256": strings.Repeat("ab", 32)})
	nopin := writeFiles(t, filepath.Join(root, "nopin"), map[string]string{"username": "fence", "password": "pw"})
	rf := func(system, creds string) string {
		return `{"redfish": {"endpoint": "https://10.0.8.17", "system": "` + system + `", "credentials": "` + creds + `"}}`
	}
	for name, body := range map[string]string{
		"no nodes":            `{"nodes": {}}`,
		"unknown field":       `{"nodes": {"a": ` + rf("/redfish/v1/Systems/1", bmc) + `}, "insecure": true}`,
		"no controller":       `{"nodes": {"a": {}}}`,
		"two controllers":     `{"nodes": {"a": {"redfish": {}, "ssh": {}}}}`,
		"missing password":    `{"nodes": {"a": ` + rf("/redfish/v1/Systems/1", nopass) + `}}`,
		"no TLS pin":          `{"nodes": {"a": ` + rf("/redfish/v1/Systems/1", nopin) + `}}`,
		"not a system":        `{"nodes": {"a": ` + rf("/redfish/v1/Managers/1", bmc) + `}}`,
		"one system, 2 nodes": `{"nodes": {"a": ` + rf("/redfish/v1/Systems/1", bmc) + `, "b": ` + rf("/redfish/v1/Systems/1", bmc) + `}}`,
	} {
		path := filepath.Join(root, "c.json")
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, _, err := LoadConfig(path); err == nil {
			t.Errorf("%s: accepted", name)
		} else if strings.Contains(err.Error(), "pw") {
			t.Errorf("%s: an error carries the password: %v", name, err)
		}
	}
}
