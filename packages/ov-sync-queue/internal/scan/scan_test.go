package scan

import (
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"testing"

	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/config"
)

func tree(t *testing.T, files ...string) string {
	t.Helper()
	dir := t.TempDir()
	for _, rel := range files {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(rel), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func rels(t *testing.T, dir string, cfg config.Sync) []string {
	t.Helper()
	files, err := Select(dir, cfg)
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, f := range files {
		out = append(out, f.Rel)
	}
	return out
}

func TestSelection(t *testing.T) {
	all := []string{
		"a.md", "b.pdf", "_state/manifest.json", "_state/x.md", "docs/c.md", "docs/drafts/d.md",
		"docs/node_modules/e.md", "notes/f.md", "notes/g.tmp.md", config.FileName, CacheFile,
	}

	t.Run("defaults", func(t *testing.T) {
		dir := tree(t, all...)
		got := rels(t, dir, config.Default().Sync)
		want := []string{"a.md", "docs/c.md", "docs/drafts/d.md", "notes/f.md", "notes/g.tmp.md"}
		if !slices.Equal(got, want) {
			t.Fatalf("got %v, want %v", got, want)
		}
	})

	t.Run("include folders, extensions and globs", func(t *testing.T) {
		dir := tree(t, all...)
		cfg := config.Default().Sync
		cfg.IncludeFolders = []string{"docs", "notes"}
		cfg.Exclude = []string{"docs/drafts/**", "**/*.tmp.md"}
		got := rels(t, dir, cfg)
		want := []string{"docs/c.md", "notes/f.md"}
		if !slices.Equal(got, want) {
			t.Fatalf("got %v, want %v", got, want)
		}
	})

	t.Run("no extension filter still keeps _state and ovsq's files out", func(t *testing.T) {
		dir := tree(t, all...)
		cfg := config.Default().Sync
		cfg.IncludeExtensions = nil
		got := rels(t, dir, cfg)
		for _, rel := range got {
			if rel == config.FileName || rel == CacheFile || rel == "_state/manifest.json" || rel == "_state/x.md" {
				t.Fatalf("selected %s: %v", rel, got)
			}
		}
		if !slices.Contains(got, "b.pdf") {
			t.Fatalf("empty extension list should keep b.pdf: %v", got)
		}
	})

	t.Run("a symlinked root is followed", func(t *testing.T) {
		dir := tree(t, "a.md", "docs/c.md")
		link := filepath.Join(t.TempDir(), "link")
		if err := os.Symlink(dir, link); err != nil {
			t.Fatal(err)
		}
		got := rels(t, link, config.Default().Sync)
		if !slices.Equal(got, []string{"a.md", "docs/c.md"}) {
			t.Fatalf("got %v", got)
		}
	})

	t.Run("a root that is a file or missing is an error", func(t *testing.T) {
		dir := tree(t, "a.md")
		for _, root := range []string{filepath.Join(dir, "a.md"), filepath.Join(dir, "nope")} {
			if _, err := Select(root, config.Default().Sync); err == nil {
				t.Fatalf("%s: want an error", root)
			}
		}
	})

	t.Run("a missing include folder is an error", func(t *testing.T) {
		dir := tree(t, all...)
		cfg := config.Default().Sync
		cfg.IncludeFolders = []string{"nope"}
		if _, err := Select(dir, cfg); err == nil {
			t.Fatal("want an error")
		}
	})

	t.Run("git_tracked_only lists tracked files only", func(t *testing.T) {
		if _, err := exec.LookPath("git"); err != nil {
			t.Skip("git not on PATH")
		}
		dir := tree(t, "tracked.md", "untracked.md", "sub/tracked2.md")
		git := func(args ...string) {
			cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
			cmd.Env = append(os.Environ(), "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_SYSTEM=/dev/null")
			if out, err := cmd.CombinedOutput(); err != nil {
				t.Fatalf("git %v: %v\n%s", args, err, out)
			}
		}
		git("init", "-q")
		git("add", "tracked.md", "sub/tracked2.md")
		cfg := config.Default().Sync
		cfg.GitTrackedOnly = true
		got := rels(t, dir, cfg)
		want := []string{"sub/tracked2.md", "tracked.md"}
		if !slices.Equal(got, want) {
			t.Fatalf("got %v, want %v", got, want)
		}
	})

	t.Run("git_tracked_only outside a repo is an error", func(t *testing.T) {
		dir := tree(t, "a.md")
		cfg := config.Default().Sync
		cfg.GitTrackedOnly = true
		t.Setenv("GIT_CEILING_DIRECTORIES", filepath.Dir(dir))
		if _, err := Select(dir, cfg); err == nil {
			t.Fatal("want an error")
		}
	})
}
