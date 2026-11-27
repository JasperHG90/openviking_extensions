package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/config"
	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/fakestore"
	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/scan"
	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/syncer"
)

const baseConfig = "[target]\nprefix = \"JasperHG90/notes\"\n"

func folder(t *testing.T, cfg string, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	writeFiles(t, dir, files)
	if err := os.WriteFile(filepath.Join(dir, config.FileName), []byte(cfg), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func writeFiles(t *testing.T, dir string, files map[string]string) {
	t.Helper()
	for rel, body := range files {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// ovsq runs the CLI against store and returns its exit code and stdout.
func ovsq(t *testing.T, store *fakestore.Store, args ...string) (int, string) {
	t.Helper()
	var out, errOut bytes.Buffer
	code := run(context.Background(), args, &out, &errOut, func(config.Config) (syncer.Store, error) { return store, nil })
	if errOut.Len() > 0 {
		t.Logf("stderr: %s", errOut.String())
	}
	return code, out.String()
}

func sha(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

func manifest(t *testing.T, store *fakestore.Store, key string) map[string]string {
	t.Helper()
	raw, ok := store.Snapshot()[key]
	if !ok {
		t.Fatalf("no manifest at %s", key)
	}
	m := map[string]string{}
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("manifest is not path-to-sha256 JSON: %v\n%s", err, raw)
	}
	return m
}

func puts(calls []fakestore.Call) []string {
	var keys []string
	for _, c := range calls {
		if c.Op == "PUT" {
			keys = append(keys, c.Key)
		}
	}
	return keys
}

func TestRunUploadsAndWritesManifest(t *testing.T) {
	files := map[string]string{"a.md": "alpha", "docs/b.md": "beta", "docs/deep/c.txt": "gamma"}
	t.Run("without subdir", func(t *testing.T) {
		dir := folder(t, baseConfig, files)
		store := fakestore.New()
		code, out := ovsq(t, store, "run", dir)
		if code != 0 {
			t.Fatalf("exit %d\n%s", code, out)
		}
		objs := store.Snapshot()
		for rel, body := range files {
			if got := string(objs["JasperHG90/notes/"+rel]); got != body {
				t.Errorf("%s: bucket holds %q, want %q", rel, got, body)
			}
		}
		m := manifest(t, store, "JasperHG90/notes/_state/ovsq-local.json")
		want := map[string]string{"a.md": sha("alpha"), "docs/b.md": sha("beta"), "docs/deep/c.txt": sha("gamma")}
		if len(m) != len(want) {
			t.Fatalf("manifest %v, want %v", m, want)
		}
		for k, v := range want {
			if m[k] != v {
				t.Errorf("manifest[%s] = %q, want %q", k, m[k], v)
			}
		}
		if _, ok := objs["JasperHG90/notes/_state/manifest.json"]; ok {
			t.Error("wrote the CI action's manifest.json")
		}
		// The CI action's format: sorted keys, one-space indent, newline.
		raw := objs["JasperHG90/notes/_state/ovsq-local.json"]
		wantRaw := "{\n \"a.md\": \"" + sha("alpha") + "\",\n \"docs/b.md\": \"" + sha("beta") + "\",\n \"docs/deep/c.txt\": \"" + sha("gamma") + "\"\n}\n"
		if string(raw) != wantRaw {
			t.Errorf("manifest bytes:\n%s\nwant:\n%s", raw, wantRaw)
		}
	})
	t.Run("with subdir and source", func(t *testing.T) {
		dir := folder(t, baseConfig+"subdir = \"inbox\"\nsource = \"laptop\"\n", files)
		store := fakestore.New()
		if code, out := ovsq(t, store, "run", dir); code != 0 {
			t.Fatalf("exit %d\n%s", code, out)
		}
		if got := string(store.Snapshot()["JasperHG90/notes/inbox/docs/b.md"]); got != "beta" {
			t.Errorf("subdir key holds %q", got)
		}
		m := manifest(t, store, "JasperHG90/notes/_state/ovsq-laptop.json")
		if m["inbox/docs/b.md"] != sha("beta") || m["docs/b.md"] != "" {
			t.Errorf("manifest paths must include the subdir: %v", m)
		}
		if _, ok := store.Snapshot()["JasperHG90/notes/_state/manifest.json"]; ok {
			t.Error("wrote the CI action's manifest.json")
		}
	})
}

func TestRunUnchangedUploadsNothing(t *testing.T) {
	dir := folder(t, baseConfig, map[string]string{"a.md": "alpha", "b/c.md": "gamma"})
	store := fakestore.New()
	if code, out := ovsq(t, store, "run", dir); code != 0 {
		t.Fatalf("first run exit %d\n%s", code, out)
	}
	store.Writes()

	if code, out := ovsq(t, store, "run", dir); code != 0 {
		t.Fatalf("second run exit %d\n%s", code, out)
	}
	if w := store.Writes(); len(w) != 0 {
		t.Fatalf("second run wrote %v", puts(w))
	}

	if err := os.Remove(filepath.Join(dir, scan.CacheFile)); err != nil {
		t.Fatal(err)
	}
	if code, out := ovsq(t, store, "run", dir); code != 0 {
		t.Fatalf("run after cache loss exit %d\n%s", code, out)
	}
	if w := store.Writes(); len(w) != 0 {
		t.Fatalf("run after losing the cache wrote %v", puts(w))
	}
}

func TestRunDelete(t *testing.T) {
	dir := folder(t, baseConfig, map[string]string{"keep.md": "k", "gone.md": "g"})
	store := fakestore.New()
	if code, _ := ovsq(t, store, "run", dir); code != 0 {
		t.Fatal("first run failed")
	}
	store.Writes()
	if err := os.Remove(filepath.Join(dir, "gone.md")); err != nil {
		t.Fatal(err)
	}

	code, out := ovsq(t, store, "run", dir)
	if code != 0 {
		t.Fatalf("exit %d\n%s", code, out)
	}
	for _, c := range store.Writes() {
		if c.Op == "DELETE" {
			t.Fatalf("deleted %s without --delete", c.Key)
		}
	}
	if !strings.Contains(out, "Re-run with --delete") {
		t.Errorf("did not report the gone file:\n%s", out)
	}
	if _, ok := store.Snapshot()["JasperHG90/notes/gone.md"]; !ok {
		t.Fatal("key removed without --delete")
	}

	code, out = ovsq(t, store, "run", dir, "--delete")
	if code != 0 {
		t.Fatalf("exit %d\n%s", code, out)
	}
	if _, ok := store.Snapshot()["JasperHG90/notes/gone.md"]; ok {
		t.Fatal("--delete left the key")
	}
	m := manifest(t, store, "JasperHG90/notes/_state/ovsq-local.json")
	if _, ok := m["gone.md"]; ok || m["keep.md"] != sha("k") {
		t.Fatalf("manifest after --delete: %v", m)
	}
}

func TestStatusWritesNothing(t *testing.T) {
	dir := folder(t, baseConfig, map[string]string{"a.md": "alpha", "b.md": "beta", "old.md": "o"})
	store := fakestore.New()
	if code, _ := ovsq(t, store, "run", dir); code != 0 {
		t.Fatal("first run failed")
	}
	writeFiles(t, dir, map[string]string{"a.md": "changed", "new.md": "n"})
	if err := os.Remove(filepath.Join(dir, "old.md")); err != nil {
		t.Fatal(err)
	}
	cachePath := filepath.Join(dir, scan.CacheFile)
	before, err := os.ReadFile(cachePath)
	if err != nil {
		t.Fatal(err)
	}
	store.Writes()

	code, out := ovsq(t, store, "status", dir)
	if code != 0 {
		t.Fatalf("exit %d\n%s", code, out)
	}
	if w := store.Writes(); len(w) != 0 {
		t.Fatalf("status wrote %v", w)
	}
	after, err := os.ReadFile(cachePath)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("status changed the local cache")
	}
	if !strings.Contains(out, "2 to upload, 1 to delete, 1 unchanged") {
		t.Fatalf("status counts:\n%s", out)
	}

	code, out = ovsq(t, store, "run", dir, "--delete")
	if code != 0 || !strings.Contains(out, "2 to upload, 1 to delete, 0 failed") {
		t.Fatalf("run disagrees with status (exit %d):\n%s", code, out)
	}

	t.Run("status on a fresh folder creates no cache", func(t *testing.T) {
		fresh := folder(t, baseConfig, map[string]string{"a.md": "x"})
		if code, out := ovsq(t, fakestore.New(), "status", fresh); code != 0 {
			t.Fatalf("exit %d\n%s", code, out)
		}
		if _, err := os.Stat(filepath.Join(fresh, scan.CacheFile)); !os.IsNotExist(err) {
			t.Fatal("status created the cache file")
		}
	})
}

func TestRunSkipsLargeFiles(t *testing.T) {
	big := strings.Repeat("x", syncer.MaxFileSize+1)
	dir := folder(t, baseConfig, map[string]string{"small.md": "s", "big.md": big})
	store := fakestore.New()
	code, out := ovsq(t, store, "run", dir)
	if code != 0 {
		t.Fatalf("a skipped file failed the run: exit %d\n%s", code, out)
	}
	for _, k := range puts(store.Writes()) {
		if strings.HasSuffix(k, "big.md") {
			t.Fatal("uploaded the large file")
		}
	}
	if !strings.Contains(out, "skipped big.md") {
		t.Fatalf("no skip line naming the file:\n%s", out)
	}
	if _, ok := store.Snapshot()["JasperHG90/notes/small.md"]; !ok {
		t.Fatal("small file not uploaded")
	}

	t.Run("a file that grows past the limit keeps its old copy", func(t *testing.T) {
		writeFiles(t, dir, map[string]string{"small.md": big})
		code, out := ovsq(t, store, "run", dir, "--delete")
		if code != 0 {
			t.Fatalf("exit %d\n%s", code, out)
		}
		if got := string(store.Snapshot()["JasperHG90/notes/small.md"]); got != "s" {
			t.Fatalf("old copy changed or deleted: %q", got)
		}
	})
}

func TestInitWritesStarter(t *testing.T) {
	dir := t.TempDir()
	if code, out := ovsq(t, fakestore.New(), "init", dir); code != 0 {
		t.Fatalf("exit %d\n%s", code, out)
	}
	if code, _ := ovsq(t, fakestore.New(), "init", dir); code != 2 {
		t.Fatal("init overwrote an existing config")
	}
	// The starter has no prefix, so status refuses with the key's name.
	var errOut bytes.Buffer
	code := run(context.Background(), []string{"status", dir}, &bytes.Buffer{}, &errOut, func(config.Config) (syncer.Store, error) { return fakestore.New(), nil })
	if code != 2 || !strings.Contains(errOut.String(), "target.prefix") {
		t.Fatalf("exit %d, stderr %s", code, errOut.String())
	}
}

// The CLI half of r4: a failed upload makes ovsq run exit 1 and keeps the
// file's old manifest entry.
func TestRunFailuresKeepManifestTrue(t *testing.T) {
	dir := folder(t, baseConfig, map[string]string{"a.md": "v1"})
	store := fakestore.New()
	if code, _ := ovsq(t, store, "run", dir); code != 0 {
		t.Fatal("first run failed")
	}
	writeFiles(t, dir, map[string]string{"a.md": "v2"})
	store.FailPut["JasperHG90/notes/a.md"] = true
	if code, out := ovsq(t, store, "run", dir); code != 1 {
		t.Fatalf("a failed upload exited %d, want 1\n%s", code, out)
	}
	if m := manifest(t, store, "JasperHG90/notes/_state/ovsq-local.json"); m["a.md"] != sha("v1") {
		t.Fatalf("failed upload changed the manifest: %v", m)
	}
}

func TestDeleteSafety(t *testing.T) {
	t.Run("a symlinked folder syncs and deletes nothing", func(t *testing.T) {
		dir := folder(t, baseConfig, map[string]string{"a.md": "a", "b.md": "b"})
		store := fakestore.New()
		if code, _ := ovsq(t, store, "run", dir); code != 0 {
			t.Fatal("first run failed")
		}
		store.Writes()
		link := filepath.Join(t.TempDir(), "link")
		if err := os.Symlink(dir, link); err != nil {
			t.Fatal(err)
		}
		code, out := ovsq(t, store, "run", link, "--delete")
		if code != 0 {
			t.Fatalf("exit %d\n%s", code, out)
		}
		if w := store.Writes(); len(w) != 0 {
			t.Fatalf("run through the symlink wrote %v", w)
		}
	})

	t.Run("--delete refuses when nothing is selected", func(t *testing.T) {
		dir := folder(t, baseConfig, map[string]string{"a.md": "a", "b.md": "b"})
		store := fakestore.New()
		if code, _ := ovsq(t, store, "run", dir); code != 0 {
			t.Fatal("first run failed")
		}
		store.Writes()
		for _, rel := range []string{"a.md", "b.md"} {
			if err := os.Remove(filepath.Join(dir, rel)); err != nil {
				t.Fatal(err)
			}
		}
		code, out := ovsq(t, store, "run", dir, "--delete")
		if code != 1 || !strings.Contains(out, "refusing") {
			t.Fatalf("exit %d, want 1 with a refusal\n%s", code, out)
		}
		for _, c := range store.Writes() {
			if c.Op == "DELETE" {
				t.Fatalf("deleted %s with nothing selected", c.Key)
			}
		}
	})
}

func TestVersion(t *testing.T) {
	for _, arg := range []string{"version", "--version"} {
		var out, errOut bytes.Buffer
		if code := run(context.Background(), []string{arg}, &out, &errOut, nil); code != 0 {
			t.Fatalf("%s: exit %d, stderr %q", arg, code, errOut.String())
		}
		if got := out.String(); got != "ovsq dev\n" {
			t.Errorf("%s: got %q, want %q", arg, got, "ovsq dev\n")
		}
		if code := run(context.Background(), []string{arg, "extra"}, &out, &errOut, nil); code != 2 {
			t.Errorf("%s extra: exit %d, want 2", arg, code)
		}
	}
}
