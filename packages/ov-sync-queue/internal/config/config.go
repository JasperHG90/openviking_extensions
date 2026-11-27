// Package config loads ov-sync-queue.toml: what to sync and where in the
// bucket it lands.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
	"strings"

	"github.com/BurntSushi/toml"
)

// FileName is the config file ovsq reads from the folder root.
const FileName = "ov-sync-queue.toml"

// EnvPrefix starts every override variable: OVSQ_TARGET__PREFIX overrides
// target.prefix.
const EnvPrefix = "OVSQ_"

// Target says where synced files land.
type Target struct {
	// Bucket is the S3 bucket whose webhook feeds the JetStream queue.
	Bucket string `toml:"bucket"`
	// Endpoint is the S3 endpoint URL, with its scheme.
	Endpoint string `toml:"endpoint"`
	// Region is sent with every request, so the client never asks the server
	// for the bucket's location.
	Region string `toml:"region"`
	// Prefix is <owner>/<name>, the key ov-sync-consumer maps to one
	// viking:// folder.
	Prefix string `toml:"prefix"`
	// Subdir is an optional folder under Prefix that files land in.
	Subdir string `toml:"subdir"`
	// Source names this folder's manifest, <prefix>/_state/ovsq-<source>.json,
	// so two sources never delete each other's files.
	Source string `toml:"source"`
}

// Sync says which files are synced.
type Sync struct {
	// IncludeFolders lists folders, relative to the root, to sync. Empty
	// means the whole root.
	IncludeFolders []string `toml:"include_folders"`
	// IncludeExtensions lists the extensions to sync, with their dot. Empty
	// means every extension.
	IncludeExtensions []string `toml:"include_extensions"`
	// ExcludeFolders lists folder names skipped at any depth.
	ExcludeFolders []string `toml:"exclude_folders"`
	// Exclude lists globs, relative to the root, to skip; ** crosses folders.
	Exclude []string `toml:"exclude"`
	// GitTrackedOnly lists files with git ls-files, as the CI action does,
	// so untracked and ignored files stay out.
	GitTrackedOnly bool `toml:"git_tracked_only"`
}

// Config is the whole file.
type Config struct {
	Target Target `toml:"target"`
	Sync   Sync   `toml:"sync"`
}

// Default returns the config used for every key the file leaves out.
func Default() Config {
	return Config{
		Target: Target{
			Bucket:   "ov-sync",
			Endpoint: "https://s3.lab.orangecluster.nl",
			Region:   "us-east-1",
			Source:   "local",
		},
		Sync: Sync{
			IncludeExtensions: []string{
				".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml", ".html",
				".png", ".jpg", ".jpeg", ".gif", ".webp",
			},
			ExcludeFolders: []string{
				".git", ".obsidian", ".trash", ".venv", "__pycache__", "node_modules",
			},
		},
	}
}

var (
	prefixPart = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)
	sourceRe   = regexp.MustCompile(`^[A-Za-z0-9-]+$`)
)

// Load reads root/ov-sync-queue.toml over the defaults, applies OVSQ_
// overrides from env, and validates the result. A missing file is an error:
// without a prefix there is nowhere to sync to.
func Load(root string, env []string) (Config, error) {
	cfg := Default()
	file := filepath.Join(root, FileName)
	meta, err := toml.DecodeFile(file, &cfg)
	if errors.Is(err, os.ErrNotExist) {
		return cfg, fmt.Errorf("%s not found: run `ovsq init %s` to write one", file, root)
	}
	if err != nil {
		return cfg, fmt.Errorf("%s: %w", file, err)
	}
	if undecoded := meta.Undecoded(); len(undecoded) > 0 {
		return cfg, fmt.Errorf("%s: unknown key %s", file, undecoded[0])
	}
	if err := applyEnv(&cfg, env); err != nil {
		return cfg, err
	}
	return cfg, cfg.Validate()
}

// Validate refuses a config that would put files where the consumer cannot
// map them, or where they would clobber a manifest.
func (c *Config) Validate() error {
	t := &c.Target
	if t.Bucket == "" {
		return errors.New("target.bucket is empty")
	}
	if !strings.HasPrefix(t.Endpoint, "http://") && !strings.HasPrefix(t.Endpoint, "https://") {
		return fmt.Errorf("target.endpoint %q needs an http:// or https:// scheme", t.Endpoint)
	}
	parts := strings.Split(strings.Trim(t.Prefix, "/"), "/")
	if len(parts) != 2 || !prefixPart.MatchString(parts[0]) || !prefixPart.MatchString(parts[1]) ||
		parts[0] == "." || parts[0] == ".." || parts[1] == "." || parts[1] == ".." {
		return fmt.Errorf("target.prefix %q is not <owner>/<name>, the two-part key ov-sync-consumer maps", t.Prefix)
	}
	t.Prefix = parts[0] + "/" + parts[1]
	if t.Subdir != "" {
		clean := path.Clean(strings.Trim(t.Subdir, "/"))
		if clean == "." || clean == ".." || strings.HasPrefix(clean, "../") || path.IsAbs(t.Subdir) {
			return fmt.Errorf("target.subdir %q must be a folder under the prefix", t.Subdir)
		}
		if clean == "_state" || strings.HasPrefix(clean, "_state/") {
			return fmt.Errorf("target.subdir %q is under _state/, which holds manifests and never reaches OpenViking", t.Subdir)
		}
		t.Subdir = clean
	}
	if !sourceRe.MatchString(t.Source) {
		return fmt.Errorf("target.source %q may hold only letters, digits and dashes", t.Source)
	}
	for _, f := range c.Sync.IncludeFolders {
		clean := path.Clean(filepath.ToSlash(f))
		if clean == "." || clean == ".." || strings.HasPrefix(clean, "../") || path.IsAbs(clean) {
			return fmt.Errorf("sync.include_folders entry %q must be a folder under the root", f)
		}
	}
	for _, g := range c.Sync.Exclude {
		if !validGlob(g) {
			return fmt.Errorf("sync.exclude entry %q is not a valid glob", g)
		}
	}
	return nil
}

// ManifestKey is this source's manifest object.
func (c *Config) ManifestKey() string {
	return c.Target.Prefix + "/_state/ovsq-" + c.Target.Source + ".json"
}

// ManifestPath turns a root-relative file path into its manifest path, which
// is relative to the prefix and includes the subdir.
func (c *Config) ManifestPath(rel string) string {
	if c.Target.Subdir == "" {
		return rel
	}
	return c.Target.Subdir + "/" + rel
}

// ObjectKey is the bucket key for a manifest path.
func (c *Config) ObjectKey(manifestPath string) string {
	return c.Target.Prefix + "/" + manifestPath
}

// applyEnv sets each field whose OVSQ_<SECTION>__<KEY> variable is present.
// A list takes a JSON array, a bool takes true or false.
func applyEnv(cfg *Config, env []string) error {
	vars := map[string]string{}
	for _, kv := range env {
		k, v, ok := strings.Cut(kv, "=")
		if ok && strings.HasPrefix(k, EnvPrefix) {
			vars[k] = v
		}
	}
	root := reflect.ValueOf(cfg).Elem()
	for i := range root.NumField() {
		section := root.Field(i)
		sectionName := root.Type().Field(i).Tag.Get("toml")
		for j := range section.NumField() {
			key := section.Type().Field(j).Tag.Get("toml")
			name := EnvPrefix + strings.ToUpper(sectionName) + "__" + strings.ToUpper(key)
			raw, ok := vars[name]
			if !ok {
				continue
			}
			field := section.Field(j)
			switch field.Kind() {
			case reflect.String:
				field.SetString(raw)
			case reflect.Bool:
				b, err := strconv.ParseBool(raw)
				if err != nil {
					return fmt.Errorf("%s=%q is not true or false", name, raw)
				}
				field.SetBool(b)
			case reflect.Slice:
				var list []string
				if err := json.Unmarshal([]byte(raw), &list); err != nil {
					return fmt.Errorf("%s=%q is not a JSON array of strings", name, raw)
				}
				field.Set(reflect.ValueOf(list))
			}
		}
	}
	return nil
}

// Starter is the file `ovsq init` writes.
const Starter = `# ovsq config. Every key can be overridden by OVSQ_<SECTION>__<KEY>,
# e.g. OVSQ_TARGET__PREFIX. A list takes a JSON array.

[target]
bucket = "ov-sync"
endpoint = "https://s3.lab.orangecluster.nl"
region = "us-east-1"
# <owner>/<name>. ov-sync-consumer maps it to a viking:// folder; a prefix
# missing from its prefix map reaches the bucket and goes no further.
prefix = ""
# Optional folder under the prefix that files land in.
subdir = ""
# Names this folder's manifest, <prefix>/_state/ovsq-<source>.json. Give each
# folder that syncs into one prefix its own source.
source = "local"

[sync]
include_folders = []          # e.g. ["docs", ".okf"]; empty means everything
include_extensions = [".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml", ".html", ".png", ".jpg", ".jpeg", ".gif", ".webp"]
exclude_folders = [".git", ".obsidian", ".trash", ".venv", "__pycache__", "node_modules"]
exclude = []                  # globs, e.g. "drafts/**", "*.tmp.md"
git_tracked_only = false      # true: only files git ls-files lists
`
