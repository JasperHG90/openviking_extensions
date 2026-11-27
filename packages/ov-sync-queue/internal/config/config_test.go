package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func write(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, FileName), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestConfig(t *testing.T) {
	t.Run("defaults fill what the file leaves out", func(t *testing.T) {
		dir := write(t, "[target]\nprefix = \"JasperHG90/notes\"\n")
		cfg, err := Load(dir, nil)
		if err != nil {
			t.Fatal(err)
		}
		if cfg.Target.Bucket != "ov-sync" || cfg.Target.Source != "local" || cfg.Target.Region != "us-east-1" {
			t.Fatalf("defaults not applied: %+v", cfg.Target)
		}
		if len(cfg.Sync.IncludeExtensions) == 0 || len(cfg.Sync.ExcludeFolders) == 0 {
			t.Fatalf("sync defaults not applied: %+v", cfg.Sync)
		}
		if got := cfg.ManifestKey(); got != "JasperHG90/notes/_state/ovsq-local.json" {
			t.Fatalf("manifest key %q", got)
		}
	})

	t.Run("the starter file loads once a prefix is set", func(t *testing.T) {
		dir := write(t, strings.Replace(Starter, `prefix = ""`, `prefix = "a/b"`, 1))
		if _, err := Load(dir, nil); err != nil {
			t.Fatal(err)
		}
	})

	t.Run("OVSQ_ variables override the file", func(t *testing.T) {
		dir := write(t, "[target]\nprefix = \"a/b\"\n[sync]\ngit_tracked_only = false\n")
		cfg, err := Load(dir, []string{
			"OVSQ_TARGET__PREFIX=JasperHG90/other",
			"OVSQ_TARGET__SUBDIR=notes/inbox",
			"OVSQ_SYNC__INCLUDE_FOLDERS=[\"docs\",\".okf\"]",
			"OVSQ_SYNC__GIT_TRACKED_ONLY=true",
			"UNRELATED=1",
		})
		if err != nil {
			t.Fatal(err)
		}
		if cfg.Target.Prefix != "JasperHG90/other" || cfg.Target.Subdir != "notes/inbox" {
			t.Fatalf("target overrides: %+v", cfg.Target)
		}
		if strings.Join(cfg.Sync.IncludeFolders, ",") != "docs,.okf" || !cfg.Sync.GitTrackedOnly {
			t.Fatalf("sync overrides: %+v", cfg.Sync)
		}
		if got := cfg.ObjectKey(cfg.ManifestPath("a.md")); got != "JasperHG90/other/notes/inbox/a.md" {
			t.Fatalf("object key %q", got)
		}
	})

	t.Run("a bad override names its variable", func(t *testing.T) {
		dir := write(t, "[target]\nprefix = \"a/b\"\n")
		_, err := Load(dir, []string{"OVSQ_SYNC__EXCLUDE=docs"})
		if err == nil || !strings.Contains(err.Error(), "OVSQ_SYNC__EXCLUDE") {
			t.Fatalf("want an error naming the variable, got %v", err)
		}
	})

	refusals := map[string]struct{ body, key string }{
		"empty prefix":     {"[target]\nprefix = \"\"\n", "target.prefix"},
		"one-part prefix":  {"[target]\nprefix = \"notes\"\n", "target.prefix"},
		"three-part":       {"[target]\nprefix = \"a/b/c\"\n", "target.prefix"},
		"dotdot prefix":    {"[target]\nprefix = \"../b\"\n", "target.prefix"},
		"source with dot":  {"[target]\nprefix = \"a/b\"\nsource = \"my.laptop\"\n", "target.source"},
		"empty source":     {"[target]\nprefix = \"a/b\"\nsource = \"\"\n", "target.source"},
		"subdir escapes":   {"[target]\nprefix = \"a/b\"\nsubdir = \"../x\"\n", "target.subdir"},
		"subdir in state":  {"[target]\nprefix = \"a/b\"\nsubdir = \"_state/x\"\n", "target.subdir"},
		"no scheme":        {"[target]\nprefix = \"a/b\"\nendpoint = \"s3.example\"\n", "target.endpoint"},
		"unknown key":      {"[target]\nprefix = \"a/b\"\nprefx = \"x\"\n", "prefx"},
		"folder escapes":   {"[target]\nprefix = \"a/b\"\n[sync]\ninclude_folders = [\"../up\"]\n", "sync.include_folders"},
		"bad exclude glob": {"[target]\nprefix = \"a/b\"\n[sync]\nexclude = [\"[\"]\n", "sync.exclude"},
	}
	for name, c := range refusals {
		t.Run("refuses "+name, func(t *testing.T) {
			_, err := Load(write(t, c.body), nil)
			if err == nil || !strings.Contains(err.Error(), c.key) {
				t.Fatalf("want an error naming %s, got %v", c.key, err)
			}
		})
	}

	t.Run("a missing file says how to make one", func(t *testing.T) {
		_, err := Load(t.TempDir(), nil)
		if err == nil || !strings.Contains(err.Error(), "ovsq init") {
			t.Fatalf("got %v", err)
		}
	})
}
