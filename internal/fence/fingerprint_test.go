package fence

import (
	"os"
	"path/filepath"
	"testing"
)

func TestARotatedPasswordChangesTheFingerprint(t *testing.T) {
	dir := t.TempDir()
	write := func(name, v string) {
		t.Helper()
		if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, name)), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), []byte(v), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("fence.json", `{"nodes":{}}`)
	write("bmc/password", "old")
	a, err := Fingerprint(dir)
	if err != nil {
		t.Fatal(err)
	}
	if b, _ := Fingerprint(dir); b != a {
		t.Fatal("the same contents gave another fingerprint")
	}
	write("bmc/password", "new")
	if b, _ := Fingerprint(dir); b == a {
		t.Fatal("a rotated password left the fingerprint unchanged")
	}
}

func TestASecretVolumeIsFingerprintedByItsDataLink(t *testing.T) {
	dir := t.TempDir()
	for _, d := range []string{"..2026_10_09_01", "..2026_10_09_02"} {
		if err := os.MkdirAll(filepath.Join(dir, d), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	link := func(target string) {
		t.Helper()
		_ = os.Remove(filepath.Join(dir, "..data"))
		if err := os.Symlink(target, filepath.Join(dir, "..data")); err != nil {
			t.Fatal(err)
		}
	}
	link("..2026_10_09_01")
	a, _ := Fingerprint(dir)
	link("..2026_10_09_02") // the kubelet's atomic update
	if b, _ := Fingerprint(dir); b == a || a == "" {
		t.Fatalf("the swap went unseen: %q %q", a, b)
	}
}
