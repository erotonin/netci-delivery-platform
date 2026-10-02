package cellagent

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func touch(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, nil, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestPluginsTheImageDoesNotCarryAreRemovedFromJenkinsHome(t *testing.T) {
	home, ref := t.TempDir(), t.TempDir()
	for _, f := range []string{"git.jpi", "netci.jpi.override", "workflow-job.jpi"} {
		touch(t, filepath.Join(ref, "plugins", f))
	}
	for _, f := range []string{"git.jpi", "netci.jpi", "workflow-job.jpi", "workflow-job.jpi.pinned",
		"opentelemetry.jpi", "opentelemetry.jpi.pinned", "opentelemetry.bak", "opentelemetry/META-INF/MANIFEST.MF",
		"legacy.hpi", "README.txt"} {
		touch(t, filepath.Join(home, "plugins", f))
	}
	removed, err := PrunePlugins(home, ref)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(removed, []string{"legacy", "opentelemetry"}) {
		t.Fatalf("removed %v", removed)
	}
	for _, gone := range []string{"opentelemetry.jpi", "opentelemetry.jpi.pinned", "opentelemetry.bak", "opentelemetry", "legacy.hpi"} {
		if _, err := os.Stat(filepath.Join(home, "plugins", gone)); !os.IsNotExist(err) {
			t.Fatalf("%s still there", gone)
		}
	}
	for _, kept := range []string{"git.jpi", "netci.jpi", "workflow-job.jpi", "workflow-job.jpi.pinned", "README.txt"} {
		if _, err := os.Stat(filepath.Join(home, "plugins", kept)); err != nil {
			t.Fatalf("%s removed: %v", kept, err)
		}
	}
}

func TestAnImageWithoutPluginsInRefRemovesNothing(t *testing.T) {
	home := t.TempDir()
	touch(t, filepath.Join(home, "plugins", "git.jpi"))
	removed, err := PrunePlugins(home, filepath.Join(t.TempDir(), "missing"))
	if err != nil || removed != nil {
		t.Fatalf("removed %v, %v", removed, err)
	}
	if _, err := os.Stat(filepath.Join(home, "plugins", "git.jpi")); err != nil {
		t.Fatal(err)
	}
}
