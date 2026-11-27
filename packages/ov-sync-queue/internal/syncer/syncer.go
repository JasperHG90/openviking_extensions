// Package syncer mirrors selected files into one prefix of a bucket, using a
// manifest, the way the openviking-sync CI action does.
//
// The manifest, <prefix>/_state/ovsq-<source>.json, maps each synced path
// (relative to the prefix, subdir included) to its sha256. A run uploads files
// whose hash differs from it and, with Delete, removes keys it lists whose file
// is gone, so the credential never needs to list the bucket. It writes the
// manifest as the bucket now stands: a failed upload keeps its old entry and a
// failed delete keeps its entry, so the next run retries exactly those.
// Uploads go in batches, and the manifest is written after every batch, so a
// run killed partway keeps its progress.
package syncer

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/config"
	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/scan"
	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/state"
)

// MaxFileSize is the largest file ovsq uploads. minio-go switches to a
// multipart upload above 16 MiB, which was never tried against SeaweedFS
// (spike ov-sync-go/1), and OpenViking refuses files over 8 MB anyway.
const MaxFileSize = 16 << 20

// ErrNotFound is what Store.Get returns for a missing key.
var ErrNotFound = errors.New("key not found")

// Store is the three calls the CI credential allows: get, put and delete one
// key. There is no list.
type Store interface {
	Get(ctx context.Context, key string) ([]byte, error)
	Put(ctx context.Context, key string, body []byte) error
	Delete(ctx context.Context, key string) error
}

// Manifest maps a manifest path to its sha256.
type Manifest map[string]string

// Upload is one file to send.
type Upload struct {
	Rel          string // relative to the root
	ManifestPath string // relative to the prefix
	SHA256       string // as hashed during the scan
}

// Plan is what a run would do.
type Plan struct {
	Uploads   []Upload
	Deletes   []string // manifest paths whose file is gone
	Skipped   []string // root-relative paths over MaxFileSize
	Failed    []string // root-relative paths that could not be hashed
	Unchanged int
	Old       Manifest
	selected  map[string]bool // root-relative paths the scan found
}

// Engine holds what a sync needs.
type Engine struct {
	Root  string
	Cfg   config.Config
	Store Store
	Cache *state.Cache
	Hash  state.Hasher
	Out   io.Writer
	// BatchSize is how many uploads go between manifest checkpoints.
	BatchSize int
	// Workers is how many requests run at once.
	Workers int
}

func (e *Engine) printf(format string, args ...any) {
	fmt.Fprintf(e.Out, format+"\n", args...)
}

// ReadManifest returns the manifest, or an empty one when it does not exist.
func ReadManifest(ctx context.Context, s Store, key string) (Manifest, error) {
	raw, err := s.Get(ctx, key)
	if errors.Is(err, ErrNotFound) {
		return Manifest{}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", key, err)
	}
	m := Manifest{}
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("%s is not a path-to-sha256 JSON object: %w", key, err)
	}
	return m, nil
}

// EncodeManifest writes m the way the CI action does: sorted keys, one-space
// indent, trailing newline.
func EncodeManifest(m Manifest) []byte {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", " ")
	_ = enc.Encode(m) // a map of strings always encodes
	return buf.Bytes()
}

// Prepare scans the root and compares it with the manifest.
func (e *Engine) Prepare(ctx context.Context) (*Plan, error) {
	files, err := scan.Select(e.Root, e.Cfg.Sync)
	if err != nil {
		return nil, err
	}
	old, err := ReadManifest(ctx, e.Store, e.Cfg.ManifestKey())
	if err != nil {
		return nil, err
	}
	p := &Plan{Old: old, selected: map[string]bool{}}
	keep := map[string]bool{} // manifest paths that must not be deleted
	for _, f := range files {
		p.selected[f.Rel] = true
		mp := e.Cfg.ManifestPath(f.Rel)
		keep[mp] = true
		if f.Size > MaxFileSize {
			p.Skipped = append(p.Skipped, f.Rel)
			continue
		}
		sum, err := e.Cache.Hash(f.Rel, filepath.Join(e.Root, filepath.FromSlash(f.Rel)), f.Size, f.ModTime, e.Hash)
		if err != nil {
			p.Failed = append(p.Failed, f.Rel)
			continue
		}
		if old[mp] == sum {
			p.Unchanged++
			continue
		}
		p.Uploads = append(p.Uploads, Upload{Rel: f.Rel, ManifestPath: mp, SHA256: sum})
	}
	for mp := range old {
		if !keep[mp] {
			p.Deletes = append(p.Deletes, mp)
		}
	}
	sort.Strings(p.Deletes)
	return p, nil
}

// Report prints what a plan would do, without doing it.
func (e *Engine) Report(p *Plan) {
	for _, u := range p.Uploads {
		e.printf("would upload %s", e.Cfg.ObjectKey(u.ManifestPath))
	}
	for _, mp := range p.Deletes {
		e.printf("would delete %s (only with --delete)", e.Cfg.ObjectKey(mp))
	}
	e.reportSkips(p)
	e.printf("%d to upload, %d to delete, %d unchanged", len(p.Uploads), len(p.Deletes), p.Unchanged)
}

func (e *Engine) reportSkips(p *Plan) {
	for _, rel := range p.Skipped {
		e.printf("skipped %s: over the %d MiB limit", rel, MaxFileSize>>20)
	}
	for _, rel := range p.Failed {
		e.printf("error: could not read %s", rel)
	}
}

// Result counts what a run did.
type Result struct {
	Uploaded, Deleted, Failed int
}

// Run carries out a plan. With del, it removes keys whose file is gone;
// without it, it only reports them. Per-file and manifest failures are
// counted in Result.Failed; any failure means the run should exit non-zero.
func (e *Engine) Run(ctx context.Context, p *Plan, del bool) Result {
	key := e.Cfg.ManifestKey()
	// Manifest writes ignore cancellation: after Ctrl-C the manifest must
	// still record the uploads that finished.
	keepCtx := context.WithoutCancel(ctx)
	cur := maps.Clone(p.Old)
	res := Result{Failed: len(p.Failed)}
	e.reportSkips(p)

	batch := max(e.BatchSize, 1)
	for start := 0; start < len(p.Uploads); start += batch {
		end := min(start+batch, len(p.Uploads))
		for _, r := range e.uploadBatch(ctx, p.Uploads[start:end]) {
			if r.err != nil {
				res.Failed++
				e.printf("error: upload of %s failed: %v", r.up.Rel, r.err)
				continue
			}
			cur[r.up.ManifestPath] = r.sha
			res.Uploaded++
			e.printf("uploaded %s", e.Cfg.ObjectKey(r.up.ManifestPath))
		}
		if end < len(p.Uploads) {
			if err := e.Store.Put(keepCtx, key, EncodeManifest(cur)); err != nil {
				e.printf("error: writing the manifest checkpoint failed: %v", err)
			}
		}
	}

	if del && len(p.selected) == 0 && len(p.Deletes) > 0 {
		// An empty scan against a non-empty manifest is far likelier a wrong
		// folder or config than a folder emptied on purpose.
		res.Failed++
		e.printf("error: no files selected, so --delete would remove all %d synced keys; refusing. Check the folder and sync.include_folders.", len(p.Deletes))
		del = false
	}
	if del {
		for _, r := range e.deleteAll(ctx, p.Deletes) {
			if r.err != nil {
				res.Failed++
				e.printf("error: delete of %s failed: %v", e.Cfg.ObjectKey(r.mp), r.err)
				continue
			}
			delete(cur, r.mp)
			res.Deleted++
			e.printf("deleted %s", e.Cfg.ObjectKey(r.mp))
		}
	} else if len(p.Deletes) > 0 {
		e.printf("Gone from the folder: %d still in the bucket. Re-run with --delete to remove them.", len(p.Deletes))
	}

	toDelete := 0
	if del {
		toDelete = len(p.Deletes)
	}
	e.printf("%d to upload, %d to delete, %d failed", len(p.Uploads), toDelete, res.Failed)
	if !maps.Equal(cur, p.Old) {
		if err := e.Store.Put(keepCtx, key, EncodeManifest(cur)); err != nil {
			res.Failed++
			e.printf("error: writing the manifest failed: %v", err)
		}
	}
	if err := e.Cache.Save(p.selected); err != nil {
		e.printf("warning: saving the local cache failed: %v", err)
	}
	return res
}

type uploadResult struct {
	up  Upload
	sha string
	err error
}

// uploadBatch sends ups with up to Workers at once, in input order.
func (e *Engine) uploadBatch(ctx context.Context, ups []Upload) []uploadResult {
	out := make([]uploadResult, len(ups))
	e.parallel(len(ups), func(i int) {
		u := ups[i]
		out[i].up = u
		body, err := os.ReadFile(filepath.Join(e.Root, filepath.FromSlash(u.Rel)))
		if err != nil {
			out[i].err = err
			return
		}
		if len(body) > MaxFileSize {
			out[i].err = fmt.Errorf("grew past the %d MiB limit during the run", MaxFileSize>>20)
			return
		}
		// Record the hash of the bytes actually sent: a file edited since the
		// scan must not be recorded with its old hash.
		sum := sha256.Sum256(body)
		out[i].sha = hex.EncodeToString(sum[:])
		out[i].err = e.Store.Put(ctx, e.Cfg.ObjectKey(u.ManifestPath), body)
	})
	return out
}

type deleteResult struct {
	mp  string
	err error
}

func (e *Engine) deleteAll(ctx context.Context, mps []string) []deleteResult {
	out := make([]deleteResult, len(mps))
	e.parallel(len(mps), func(i int) {
		out[i] = deleteResult{mp: mps[i], err: e.Store.Delete(ctx, e.Cfg.ObjectKey(mps[i]))}
	})
	return out
}

func (e *Engine) parallel(n int, fn func(i int)) {
	sem := make(chan struct{}, max(e.Workers, 1))
	var wg sync.WaitGroup
	for i := range n {
		wg.Add(1)
		sem <- struct{}{}
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			fn(i)
		}()
	}
	wg.Wait()
}
