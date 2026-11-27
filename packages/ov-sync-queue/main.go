// Command ovsq mirrors a local folder into one prefix of the ov-sync bucket.
// The bucket's webhook puts each change on a JetStream queue, and
// ov-sync-consumer writes it to OpenViking. ovsq never talks to OpenViking.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"

	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/config"
	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/s3store"
	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/scan"
	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/state"
	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/syncer"
)

const usage = `ovsq mirrors a local folder into one prefix of the ov-sync bucket, where
ov-sync-consumer picks each change up from a queue and writes it to OpenViking.

Usage:
  ovsq init   [FOLDER]            write a starter ov-sync-queue.toml
  ovsq status [FOLDER]            show what the next run would do; writes nothing
  ovsq run    [FOLDER] [--delete] upload what changed; --delete also removes
                                  keys whose file is gone
  ovsq version                    print the version (or --version)

FOLDER defaults to the current folder and must hold ov-sync-queue.toml (except
for init). Every config key can be overridden by OVSQ_<SECTION>__<KEY>, e.g.
OVSQ_TARGET__PREFIX=JasperHG90/notes.

S3 credentials come from AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and
AWS_SESSION_TOKEN, then MINIO_ROOT_USER and MINIO_ROOT_PASSWORD, then the AWS
shared credentials file (AWS_PROFILE picks the profile).

Exit status: 0 when every file synced; 1 when an upload, delete or manifest
write failed (re-run to retry exactly those), the folder, an include folder,
git or the manifest could not be read, or --delete was refused because no
files were selected; 2 on a usage or config file error.
`

// version is stamped by the release build (-ldflags "-X main.version=1.2.3").
var version = "dev"

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	os.Exit(run(ctx, os.Args[1:], os.Stdout, os.Stderr, nil))
}

// newStore is swapped in tests.
type storeFactory func(cfg config.Config) (syncer.Store, error)

func s3Factory(cfg config.Config) (syncer.Store, error) {
	return s3store.New(cfg.Target.Endpoint, cfg.Target.Region, cfg.Target.Bucket, s3store.Credentials())
}

func run(ctx context.Context, args []string, stdout, stderr io.Writer, newStore storeFactory) int {
	if newStore == nil {
		newStore = s3Factory
	}
	if len(args) == 0 || args[0] == "-h" || args[0] == "--help" || args[0] == "help" {
		fmt.Fprint(stdout, usage)
		if len(args) == 0 {
			return 2
		}
		return 0
	}
	if args[0] == "version" || args[0] == "--version" {
		if len(args) > 1 {
			fmt.Fprintf(stderr, "ovsq %s takes no arguments\n", args[0])
			return 2
		}
		fmt.Fprintln(stdout, "ovsq", version)
		return 0
	}
	cmd, rest := args[0], args[1:]
	fs := flag.NewFlagSet("ovsq "+cmd, flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() { fmt.Fprint(stderr, usage) }
	del := false
	if cmd == "run" {
		fs.BoolVar(&del, "delete", false, "also remove bucket keys whose file is gone from the folder")
	}
	// The flag package stops at the first argument that is not a flag, so
	// parse again after each one: `ovsq run DIR --delete` works too.
	var positional []string
	for {
		if err := fs.Parse(rest); err != nil {
			if errors.Is(err, flag.ErrHelp) {
				return 0
			}
			return 2
		}
		if fs.NArg() == 0 {
			break
		}
		positional = append(positional, fs.Arg(0))
		rest = fs.Args()[1:]
	}
	folder := "."
	switch len(positional) {
	case 0:
	case 1:
		folder = positional[0]
	default:
		fmt.Fprintf(stderr, "ovsq %s takes one FOLDER, got %d arguments\n", cmd, len(positional))
		return 2
	}

	switch cmd {
	case "init":
		return initCmd(folder, stdout, stderr)
	case "status", "run":
	default:
		fmt.Fprintf(stderr, "unknown command %q: use init, status, run or version\n", cmd)
		return 2
	}

	cfg, err := config.Load(folder, os.Environ())
	if err != nil {
		fmt.Fprintln(stderr, "error:", err)
		return 2
	}
	store, err := newStore(cfg)
	if err != nil {
		fmt.Fprintln(stderr, "error:", err)
		return 2
	}
	readOnly := cmd == "status"
	cache, err := state.Open(filepath.Join(folder, scan.CacheFile), readOnly)
	if err != nil {
		fmt.Fprintln(stderr, "error: opening the local cache:", err)
		return 2
	}
	defer cache.Close()

	eng := &syncer.Engine{
		Root: folder, Cfg: cfg, Store: store, Cache: cache, Hash: state.SHA256File,
		Out: stdout, BatchSize: 100, Workers: 8,
	}
	plan, err := eng.Prepare(ctx)
	if err != nil {
		fmt.Fprintln(stderr, "error:", err)
		return 1
	}
	if readOnly {
		eng.Report(plan)
		return 0
	}
	if res := eng.Run(ctx, plan, del); res.Failed > 0 {
		return 1
	}
	return 0
}

func initCmd(folder string, stdout, stderr io.Writer) int {
	path := filepath.Join(folder, config.FileName)
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if errors.Is(err, os.ErrExist) {
		fmt.Fprintf(stderr, "error: %s already exists; edit it instead\n", path)
		return 2
	}
	if err != nil {
		fmt.Fprintln(stderr, "error:", err)
		return 2
	}
	defer f.Close()
	if _, err := f.WriteString(config.Starter); err != nil {
		fmt.Fprintln(stderr, "error:", err)
		return 2
	}
	fmt.Fprintf(stdout, "wrote %s: set target.prefix, then run `ovsq status %s`\n", path, folder)
	return 0
}
