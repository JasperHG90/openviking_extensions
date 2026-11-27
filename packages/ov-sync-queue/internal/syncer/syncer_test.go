package syncer_test

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/config"
	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/fakestore"
	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/scan"
	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/state"
	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/syncer"
)

const manifestKey = "o/n/_state/ovsq-local.json"

func engine(t *testing.T, dir string, store syncer.Store, hash state.Hasher) *syncer.Engine {
	t.Helper()
	cfg := config.Default()
	cfg.Target.Prefix = "o/n"
	cfg.Sync.IncludeExtensions = nil
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	cache, err := state.Open(filepath.Join(dir, scan.CacheFile), false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cache.Close() })
	if hash == nil {
		hash = state.SHA256File
	}
	return &syncer.Engine{Root: dir, Cfg: cfg, Store: store, Cache: cache, Hash: hash,
		Out: &bytes.Buffer{}, BatchSize: 2, Workers: 4}
}

func put(t *testing.T, dir string, files map[string]string) {
	t.Helper()
	for rel, body := range files {
		if err := os.WriteFile(filepath.Join(dir, rel), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func decode(t *testing.T, raw []byte) map[string]string {
	t.Helper()
	m := map[string]string{}
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func runOnce(t *testing.T, e *syncer.Engine, del bool) syncer.Result {
	t.Helper()
	p, err := e.Prepare(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	r := e.Run(context.Background(), p, del)
	// Release the cache file, as ovsq does on exit, so the next engine can
	// open it.
	if err := e.Cache.Close(); err != nil {
		t.Fatal(err)
	}
	return r
}

func TestRunFailuresKeepManifestTrue(t *testing.T) {
	t.Run("a failed upload keeps its old entry and fails the run", func(t *testing.T) {
		dir := t.TempDir()
		store := fakestore.New()
		put(t, dir, map[string]string{"a.md": "v1", "b.md": "b"})
		if r := runOnce(t, engine(t, dir, store, nil), false); r.Failed != 0 {
			t.Fatalf("first run: %+v", r)
		}
		old := decode(t, store.Snapshot()[manifestKey])

		put(t, dir, map[string]string{"a.md": "v2", "c.md": "c"})
		store.FailPut["o/n/a.md"] = true
		store.FailPut["o/n/c.md"] = true
		r := runOnce(t, engine(t, dir, store, nil), false)
		if r.Failed != 2 {
			t.Fatalf("want 2 failures, got %+v", r)
		}
		m := decode(t, store.Snapshot()[manifestKey])
		if m["a.md"] != old["a.md"] {
			t.Errorf("failed upload changed its entry: %q -> %q", old["a.md"], m["a.md"])
		}
		if _, ok := m["c.md"]; ok {
			t.Error("failed new upload got an entry")
		}

		// The next run retries exactly the failed files.
		delete(store.FailPut, "o/n/a.md")
		delete(store.FailPut, "o/n/c.md")
		store.Writes()
		if r := runOnce(t, engine(t, dir, store, nil), false); r.Failed != 0 || r.Uploaded != 2 {
			t.Fatalf("retry run: %+v", r)
		}
	})

	t.Run("a failed delete keeps its entry", func(t *testing.T) {
		dir := t.TempDir()
		store := fakestore.New()
		put(t, dir, map[string]string{"a.md": "a", "b.md": "b"})
		runOnce(t, engine(t, dir, store, nil), false)
		if err := os.Remove(filepath.Join(dir, "b.md")); err != nil {
			t.Fatal(err)
		}
		store.FailDelete["o/n/b.md"] = true
		r := runOnce(t, engine(t, dir, store, nil), true)
		if r.Failed != 1 {
			t.Fatalf("want 1 failure, got %+v", r)
		}
		if _, ok := decode(t, store.Snapshot()[manifestKey])["b.md"]; !ok {
			t.Fatal("failed delete dropped its entry")
		}
	})

	t.Run("the manifest is written after every full batch", func(t *testing.T) {
		dir := t.TempDir()
		store := fakestore.New()
		put(t, dir, map[string]string{"1.md": "1", "2.md": "2", "3.md": "3", "4.md": "4", "5.md": "5"})
		runOnce(t, engine(t, dir, store, nil), false)
		// BatchSize 2: checkpoints after files 2 and 4, then the final write.
		var sizes []int
		for _, c := range store.Writes() {
			if c.Op == "PUT" && c.Key == manifestKey {
				sizes = append(sizes, len(decode(t, c.Body)))
			}
		}
		if len(sizes) != 3 || sizes[0] != 2 || sizes[1] != 4 || sizes[2] != 5 {
			t.Fatalf("manifest writes held %v entries, want [2 4 5]", sizes)
		}
	})

	t.Run("a failed final manifest write fails the run", func(t *testing.T) {
		dir := t.TempDir()
		store := fakestore.New()
		put(t, dir, map[string]string{"a.md": "a"})
		store.FailPut[manifestKey] = true
		if r := runOnce(t, engine(t, dir, store, nil), false); r.Failed != 1 {
			t.Fatalf("want 1 failure, got %+v", r)
		}
	})
}

func TestCacheSkipsUnchanged(t *testing.T) {
	dir := t.TempDir()
	store := fakestore.New()
	put(t, dir, map[string]string{"a.md": "a", "b.md": "b"})
	hashed := map[string]int{}
	counting := func(p string) (string, error) {
		hashed[filepath.Base(p)]++
		return state.SHA256File(p)
	}
	runOnce(t, engine(t, dir, store, counting), false)
	if hashed["a.md"] != 1 || hashed["b.md"] != 1 {
		t.Fatalf("first run hashed %v", hashed)
	}

	// Change b.md's size so its stat no longer matches the cache.
	put(t, dir, map[string]string{"b.md": "b2"})
	runOnce(t, engine(t, dir, store, counting), false)
	if hashed["a.md"] != 1 {
		t.Fatalf("unchanged a.md was hashed again: %v", hashed)
	}
	if hashed["b.md"] != 2 {
		t.Fatalf("changed b.md was not hashed: %v", hashed)
	}
}

// A cancelled run still records the uploads that finished.
func TestManifestWriteSurvivesCancel(t *testing.T) {
	dir := t.TempDir()
	put(t, dir, map[string]string{"a.md": "a"})
	store := fakestore.New()
	e := engine(t, dir, &cancelStore{Store: store}, nil)
	ctx, cancel := context.WithCancel(context.Background())
	p, err := e.Prepare(ctx)
	if err != nil {
		t.Fatal(err)
	}
	cancel()
	e.Run(ctx, p, false)
	_ = e.Cache.Close()
	if _, ok := decode(t, store.Snapshot()[manifestKey])["a.md"]; !ok {
		t.Fatal("manifest lost the finished upload after cancel")
	}
}

// cancelStore fails any call whose context is done, as a real client would.
type cancelStore struct{ *fakestore.Store }

func (c *cancelStore) Put(ctx context.Context, key string, body []byte) error {
	if key != manifestKey {
		// Uploads finished before the cancel; let them through.
		return c.Store.Put(context.Background(), key, body)
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return c.Store.Put(ctx, key, body)
}
