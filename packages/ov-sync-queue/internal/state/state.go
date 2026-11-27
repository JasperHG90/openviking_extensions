// Package state is ovsq's local cache: the size, mtime and sha256 of each file
// it last hashed, so an unchanged file is never read again. The cache only
// saves work. The bucket manifest is the record of what was synced, so losing
// the cache costs re-hashing, never a re-upload or a missed delete.
package state

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"time"

	bolt "go.etcd.io/bbolt"
)

var bucket = []byte("files")

// Entry is what the cache holds for one file.
type Entry struct {
	Size    int64  `json:"size"`
	ModTime int64  `json:"mtime_ns"`
	SHA256  string `json:"sha256"`
}

// Cache maps a root-relative path to its last-hashed Entry.
type Cache struct {
	db      *bolt.DB
	entries map[string]Entry
	dirty   map[string]Entry
}

// Open reads the cache at path. With readOnly, a missing file is an empty
// cache and nothing is created, so `ovsq status` never writes.
func Open(path string, readOnly bool) (*Cache, error) {
	c := &Cache{entries: map[string]Entry{}, dirty: map[string]Entry{}}
	if readOnly {
		if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
			return c, nil
		}
	}
	db, err := bolt.Open(path, 0o644, &bolt.Options{ReadOnly: readOnly, Timeout: 5 * time.Second})
	if err != nil {
		return nil, err
	}
	c.db = db
	err = db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucket)
		if b == nil {
			return nil
		}
		return b.ForEach(func(k, v []byte) error {
			var e Entry
			if json.Unmarshal(v, &e) == nil {
				c.entries[string(k)] = e
			}
			return nil
		})
	})
	if err != nil {
		_ = db.Close()
		return nil, err
	}
	return c, nil
}

// Hasher returns a file's sha256 as hex.
type Hasher func(path string) (string, error)

// SHA256File reads the file at path and returns its sha256.
func SHA256File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// Hash returns rel's sha256, from the cache when size and mtime match it,
// else from hash, remembering the result for Save.
func (c *Cache) Hash(rel, path string, size, modTime int64, hash Hasher) (string, error) {
	if e, ok := c.entries[rel]; ok && e.Size == size && e.ModTime == modTime && e.SHA256 != "" {
		return e.SHA256, nil
	}
	sum, err := hash(path)
	if err != nil {
		return "", err
	}
	c.Remember(rel, Entry{Size: size, ModTime: modTime, SHA256: sum})
	return sum, nil
}

// Remember records an entry for Save.
func (c *Cache) Remember(rel string, e Entry) {
	c.entries[rel] = e
	c.dirty[rel] = e
}

// Save writes the remembered entries and drops entries for paths not in keep.
func (c *Cache) Save(keep map[string]bool) error {
	if c.db == nil {
		return errors.New("cache was opened read-only")
	}
	return c.db.Update(func(tx *bolt.Tx) error {
		b, err := tx.CreateBucketIfNotExists(bucket)
		if err != nil {
			return err
		}
		for rel, e := range c.dirty {
			raw, _ := json.Marshal(e)
			if err := b.Put([]byte(rel), raw); err != nil {
				return err
			}
		}
		var stale [][]byte
		if err := b.ForEach(func(k, _ []byte) error {
			if !keep[string(k)] {
				stale = append(stale, append([]byte(nil), k...))
			}
			return nil
		}); err != nil {
			return err
		}
		for _, k := range stale {
			if err := b.Delete(k); err != nil {
				return err
			}
		}
		c.dirty = map[string]Entry{}
		return nil
	})
}

// Close releases the file.
func (c *Cache) Close() error {
	if c.db == nil {
		return nil
	}
	return c.db.Close()
}
