package cellagent

import (
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
