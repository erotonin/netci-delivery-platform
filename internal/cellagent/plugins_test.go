package cellagent

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
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

// Jenkins unzips a plugin again whenever its archive's modification time differs from the one it
// recorded, and the controller image unzips its plugins when it is built: a cell's archives,
// copied from an earlier image, kept that image's times, and every start unzipped all of them
// again (about 1.2 s of each start). An archive identical to the image's is given the image's time;
// any other is left as it is, and Jenkins unzips it as before.
func TestPluginArchivesIdenticalToTheImagesTakeItsTime(t *testing.T) {
	home, ref := t.TempDir(), t.TempDir()
	write := func(dir, name, body string, when time.Time) string {
		p := filepath.Join(dir, "plugins", name)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := os.Chtimes(p, when, when); err != nil {
			t.Fatal(err)
		}
		return p
	}
	image, old := time.Date(2026, 10, 1, 17, 14, 57, 123e6, time.UTC), time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC)
	write(ref, "git.jpi", "git 5.7", image)
	same := write(home, "git.jpi", "git 5.7", old)
	write(ref, "netci.jpi.override", "netci 1", image)
	netci := write(home, "netci.jpi", "netci 1", old)
	write(ref, "mailer.jpi", "mailer 2", image)
	other := write(home, "mailer.jpi", "mailer 1", old) // same size, another version
	write(ref, "absent.jpi", "x", image)

	n, err := AlignPluginTimes(home, ref)
	if err != nil {
		t.Fatal(err)
	}
	mtime := func(p string) time.Time {
		st, err := os.Stat(p)
		if err != nil {
			t.Fatal(err)
		}
		return st.ModTime()
	}
	if n != 2 || !mtime(same).Equal(image) || !mtime(netci).Equal(image) {
		t.Fatalf("aligned %d; git %v, netci %v, want %v", n, mtime(same), mtime(netci), image)
	}
	if !mtime(other).Equal(old) {
		t.Fatalf("a different archive was given the image's time: %v", mtime(other))
	}
	if n, _ := AlignPluginTimes(home, ref); n != 0 {
		t.Fatalf("a second run aligned %d again", n)
	}
}

// The pod's plugin root (an emptyDir) is linked to the plugins the image unzipped, when the image
// has them; an image without them, or a plugin root that a previous container of the pod already
// filled, is left alone -- Jenkins unzips into it as before.
func TestThePluginRootIsLinkedToTheImagesWhenItHasOne(t *testing.T) {
	dir := t.TempDir()
	image, root := filepath.Join(dir, "image-pluginroot"), filepath.Join(dir, "cache", "plugins")
	if err := os.MkdirAll(filepath.Dir(root), 0o755); err != nil {
		t.Fatal(err)
	}
	if linked, err := LinkPluginRoot(root, image); err != nil || linked {
		t.Fatalf("an image without unzipped plugins: linked %v, %v", linked, err)
	}
	if _, err := os.Lstat(root); !os.IsNotExist(err) {
		t.Fatalf("created something at the plugin root: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(image, "git"), 0o755); err != nil {
		t.Fatal(err)
	}
	if linked, err := LinkPluginRoot(root, image); err != nil || !linked {
		t.Fatalf("linked %v, %v", linked, err)
	}
	if target, err := os.Readlink(root); err != nil || target != image {
		t.Fatalf("the plugin root points to %q (%v)", target, err)
	}
	if linked, err := LinkPluginRoot(root, image); err != nil || linked {
		t.Fatalf("a second start: linked %v, %v", linked, err)
	}
	filled := filepath.Join(dir, "filled")
	if err := os.MkdirAll(filepath.Join(filled, "git"), 0o755); err != nil {
		t.Fatal(err)
	}
	if linked, err := LinkPluginRoot(filled, image); err != nil || linked {
		t.Fatalf("a plugin root already filled: linked %v, %v", linked, err)
	}
}
