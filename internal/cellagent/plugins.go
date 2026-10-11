package cellagent

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// PrunePlugins removes from JENKINS_HOME the plugins the controller's image does not carry.
//
// The Jenkins image copies its plugins (ref/plugins) into JENKINS_HOME/plugins at every start,
// and never removes one: a plugin of an earlier image stays and loads. On the lab, rolling the
// controller back from an image with the OpenTelemetry plugin left it in JENKINS_HOME, still
// configured, and every build agent then waited 10 s for it. netCI's controller carries exactly
// its declared plugin set (ADR-059), so whatever else is in JENKINS_HOME/plugins is drift.
//
// It runs behind the gate, under the cell's Lease, like everything that writes JENKINS_HOME.
// With no plugin in ref it removes nothing: an image that does not ship its plugins this way
// is not one this rule describes.
func PrunePlugins(home, ref string) ([]string, error) {
	carried := map[string]bool{}
	refs, err := os.ReadDir(filepath.Join(ref, "plugins"))
	if err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	for _, e := range refs {
		if name, ok := pluginName(e.Name()); ok {
			carried[name] = true
		}
	}
	if len(carried) == 0 {
		return nil, nil
	}
	dir := filepath.Join(home, "plugins")
	entries, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var removed []string
	for _, e := range entries {
		name, ok := pluginName(e.Name())
		if !ok || e.IsDir() || carried[name] {
			continue
		}
		// The archive, Jenkins' markers beside it, and the directory it was exploded into.
		for _, p := range []string{e.Name(), e.Name() + ".pinned", e.Name() + ".disabled", strings.TrimSuffix(e.Name(), filepath.Ext(e.Name())) + ".bak", name} {
			if err := os.RemoveAll(filepath.Join(dir, p)); err != nil {
				return removed, fmt.Errorf("remove %s: %w", p, err)
			}
		}
		removed = append(removed, name)
	}
	sort.Strings(removed)
	return removed, nil
}

// AlignPluginTimes gives each plugin archive in JENKINS_HOME/plugins that is byte for byte the
// image's (ref/plugins) the image's modification time, and returns how many it changed.
//
// Jenkins unzips a plugin into its plugin root again whenever the archive's modification time
// differs from the one it recorded there (.timestamp2), and the controller image unzips its
// plugins when it is built (jenkins/Dockerfile.controller). The image's start script copies a
// plugin into JENKINS_HOME only when its version is newer, keeping the image's time: a cell's
// archives, copied from an earlier image, kept that image's times, so every start of its
// controller unzipped all of them again (about 1.2 s of each start). An archive that differs is
// left as it is, and Jenkins unzips it as before. The contents are compared only when the size
// matches and the time does not, which after the first start is never.
func AlignPluginTimes(home, ref string) (int, error) {
	refs, err := os.ReadDir(filepath.Join(ref, "plugins"))
	if err != nil {
		if os.IsNotExist(err) {
			return 0, nil
		}
		return 0, err
	}
	n := 0
	for _, e := range refs {
		name, ok := pluginName(e.Name())
		if !ok || e.IsDir() {
			continue
		}
		src := filepath.Join(ref, "plugins", e.Name())
		dst := filepath.Join(home, "plugins", name+".jpi") // where the start script puts it
		si, err := os.Stat(src)
		if err != nil {
			return n, err
		}
		di, err := os.Stat(dst)
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			return n, err
		}
		if di.Size() != si.Size() || di.ModTime().Equal(si.ModTime()) {
			continue
		}
		same, err := sameContent(src, dst)
		if err != nil {
			return n, err
		}
		if !same {
			continue
		}
		if err := os.Chtimes(dst, si.ModTime(), si.ModTime()); err != nil {
			return n, err
		}
		n++
	}
	return n, nil
}

// LinkPluginRoot makes root, the plugin root Jenkins is given (an emptyDir in the pod), a link to
// image, the plugins the controller image unzipped when it was built -- when image exists and
// root does not yet. An image without them, or a root a previous container of the pod already
// filled, is left alone and Jenkins unzips into it as before; with the link it unzips only the
// plugins whose archive differs from the image's (AlignPluginTimes), into the container's layer.
func LinkPluginRoot(root, image string) (bool, error) {
	if st, err := os.Stat(image); err != nil || !st.IsDir() {
		if err != nil && !os.IsNotExist(err) {
			return false, err
		}
		return false, nil
	}
	if _, err := os.Lstat(root); err == nil {
		return false, nil
	} else if !os.IsNotExist(err) {
		return false, err
	}
	if err := os.MkdirAll(filepath.Dir(root), 0o755); err != nil {
		return false, err
	}
	return true, os.Symlink(image, root)
}

func sameContent(a, b string) (bool, error) {
	x, err := os.ReadFile(a)
	if err != nil {
		return false, err
	}
	y, err := os.ReadFile(b)
	if err != nil {
		return false, err
	}
	return bytes.Equal(x, y), nil
}

// pluginName: "git.jpi", "netci.jpi.override" and "old.hpi" name plugins; nothing else does.
func pluginName(file string) (string, bool) {
	file = strings.TrimSuffix(file, ".override")
	for _, ext := range []string{".jpi", ".hpi"} {
		if strings.HasSuffix(file, ext) && len(file) > len(ext) {
			return strings.TrimSuffix(file, ext), true
		}
	}
	return "", false
}
