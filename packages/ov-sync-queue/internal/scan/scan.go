// Package scan picks the files under a root that a config selects.
package scan

import (
	"bytes"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"sort"
	"strings"

	"github.com/bmatcuk/doublestar/v4"

	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/config"
)

// CacheFile is the local cache ovsq keeps in the root. Like the config file,
// it is never synced.
const CacheFile = ".ov-sync-queue.db"

// File is one selected file. Rel is relative to the root, with forward
// slashes.
type File struct {
	Rel     string
	Size    int64
	ModTime int64 // nanoseconds
}

// Select returns the regular files under root that cfg selects, sorted by
// Rel. A symlinked root is resolved; symlinks under it are not followed.
func Select(root string, cfg config.Sync) ([]File, error) {
	// WalkDir does not follow a symlinked root, so it would find nothing,
	// and every manifest entry would look deleted. Resolve it first.
	resolved, err := filepath.EvalSymlinks(root)
	if err != nil {
		return nil, fmt.Errorf("folder %s: %w", root, err)
	}
	if info, err := os.Stat(resolved); err != nil || !info.IsDir() {
		return nil, fmt.Errorf("folder %s is not a directory", root)
	}
	root = resolved
	var tracked map[string]bool
	if cfg.GitTrackedOnly {
		if tracked, err = gitTracked(root); err != nil {
			return nil, err
		}
	}
	starts := []string{"."}
	if len(cfg.IncludeFolders) > 0 {
		starts = starts[:0]
		for _, f := range cfg.IncludeFolders {
			rel := path.Clean(filepath.ToSlash(f))
			info, err := os.Lstat(filepath.Join(root, filepath.FromSlash(rel)))
			if err != nil || !info.IsDir() {
				return nil, fmt.Errorf("sync.include_folders entry %q is not a folder under %s", f, root)
			}
			starts = append(starts, rel)
		}
	}
	excludedDir := map[string]bool{}
	for _, d := range cfg.ExcludeFolders {
		excludedDir[d] = true
	}
	exts := map[string]bool{}
	for _, e := range cfg.IncludeExtensions {
		exts[strings.ToLower(e)] = true
	}

	seen := map[string]File{}
	for _, start := range starts {
		err := filepath.WalkDir(filepath.Join(root, filepath.FromSlash(start)), func(p string, d fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			rel, err := filepath.Rel(root, p)
			if err != nil {
				return err
			}
			rel = filepath.ToSlash(rel)
			if d.IsDir() {
				if rel != "." && rel != start && excludedDir[d.Name()] {
					return filepath.SkipDir
				}
				return nil
			}
			if !d.Type().IsRegular() || !wanted(rel, cfg.Exclude, exts) {
				return nil
			}
			for _, part := range strings.Split(path.Dir(rel), "/") {
				if excludedDir[part] {
					return nil
				}
			}
			if tracked != nil && !tracked[rel] {
				return nil
			}
			info, err := d.Info()
			if err != nil {
				return err
			}
			seen[rel] = File{Rel: rel, Size: info.Size(), ModTime: info.ModTime().UnixNano()}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	files := make([]File, 0, len(seen))
	for _, f := range seen {
		files = append(files, f)
	}
	sort.Slice(files, func(i, j int) bool { return files[i].Rel < files[j].Rel })
	return files, nil
}

// wanted applies the rules that do not depend on the walk: ovsq's own files,
// _state/ (which holds manifests in the bucket), extensions and globs.
func wanted(rel string, exclude []string, exts map[string]bool) bool {
	if rel == config.FileName || rel == CacheFile || strings.HasPrefix(rel, CacheFile+".") {
		return false
	}
	if rel == "_state" || strings.HasPrefix(rel, "_state/") {
		return false
	}
	if len(exts) > 0 && !exts[strings.ToLower(path.Ext(rel))] {
		return false
	}
	for _, g := range exclude {
		if ok, _ := doublestar.Match(g, rel); ok {
			return false
		}
	}
	return true
}

// gitTracked lists the files git tracks under root, relative to root.
func gitTracked(root string) (map[string]bool, error) {
	cmd := exec.Command("git", "-C", root, "ls-files", "-z", "--", ".")
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("sync.git_tracked_only is set but git ls-files failed in %s: %s", root, strings.TrimSpace(stderr.String()))
	}
	tracked := map[string]bool{}
	for _, p := range strings.Split(string(out), "\x00") {
		if p != "" {
			tracked[p] = true
		}
	}
	return tracked, nil
}
