package fence

import (
	"crypto/sha256"
	"encoding/hex"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
)

// Fingerprint identifies what a directory of power-controller configuration holds: fence.json,
// and the credentials and pinned certificates beside it. The supervisor reads them once, at
// start, and checks the node-to-machine mapping against them; when they change -- a BMC password
// rotated, a machine added -- it must start again, or it keeps the old ones: with a rotated
// password every state query fails, reads as Unknown, and nothing is ever fenced.
//
// A Secret volume is a "..data" symlink the kubelet swaps atomically: its target is the
// fingerprint. Any other directory is fingerprinted by the contents of its files.
func Fingerprint(dir string) (string, error) {
	if target, err := os.Readlink(filepath.Join(dir, "..data")); err == nil {
		return "secret:" + target, nil
	}
	var files []string
	err := filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() {
			files = append(files, p)
		}
		return nil
	})
	if err != nil {
		return "", err
	}
	sort.Strings(files)
	h := sha256.New()
	for _, f := range files {
		b, err := os.ReadFile(f)
		if err != nil {
			return "", err
		}
		h.Write([]byte(f))
		h.Write([]byte{0})
		h.Write(b)
		h.Write([]byte{0})
	}
	return "files:" + hex.EncodeToString(h.Sum(nil)), nil
}
