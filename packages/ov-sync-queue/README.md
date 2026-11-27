# ov-sync-queue

Mirror a local folder into the `ov-sync` bucket, so OpenViking gets it through
the queue instead of straight from your laptop.

`ovsq` is the local cousin of the `openviking-sync` GitHub action in
JasperHG90/localstack. It uploads files to one prefix of the SeaweedFS bucket
`ov-sync`. Each upload fires the bucket's webhook, a receiver puts it on the
JetStream stream `OV_SYNC`, and `ov-sync-consumer` writes it to OpenViking
behind a worker pool, a rate limit, retries and a dead-letter subject.
`ov-sync` (the Python package next door) writes to OpenViking directly and
gets none of that.

```console
$ ovsq init ~/notes                  # writes ov-sync-queue.toml; set target.prefix
$ ovsq status ~/notes                # what would change; writes nothing
$ ovsq run ~/notes                   # upload new and changed files
$ ovsq run ~/notes --delete          # also remove keys whose file is gone
```

## Install

Needs Go 1.26 or later.

```bash
git clone https://github.com/JasperHG90/openviking_extensions
cd openviking_extensions/packages/ov-sync-queue
go build -o ~/.local/bin/ovsq .
```

## Before files reach OpenViking

`ovsq` delivers files into the bucket. Two things outside this repo decide
whether they go further, both in JasperHG90/localstack:

1. **The prefix map.** Add your prefix to `prefixes` in
   `nomad_job.ov_sync_consumer` (`deployments/applications/services.tf`),
   mapped to a `viking://` folder. A prefix missing there reaches the bucket
   and goes no further.
2. **A write credential.** The bucket has no writers by default. You need S3
   credentials allowed to `GetObject`, `PutObject` and `DeleteObject` under
   `ov-sync/<prefix>/*`. `ovsq` never lists the bucket, so it needs no List.

## How it works

It uses the CI action's logic:

1. Pick the files the config selects.
2. Hash each one, skipping files whose size and mtime match the local cache
   (`.ov-sync-queue.db`).
3. Read this source's manifest, `<prefix>/_state/ovsq-<source>.json`, which
   maps each synced path to its sha256. A missing manifest is empty.
4. Upload files whose hash differs, in batches of 100, writing the manifest
   after each batch, so a run you stop keeps its progress.
5. With `--delete`, remove keys the manifest lists whose file is gone. If the
   scan selected no files at all, it removes nothing and exits 1: an empty
   scan is likelier a wrong folder than an emptied one.
6. Write the manifest as the bucket now stands. A failed upload keeps its old
   entry and a failed delete keeps its entry, so the next run retries exactly
   those, and the run exits 1.

The manifest in the bucket is the record of truth. Delete the local cache and
the next run re-hashes everything but uploads nothing that has not changed.

### Sharing a prefix

Each source owns its own manifest, so `ovsq` and the CI action (which owns
`_state/manifest.json`) never delete each other's files, and neither do two
folders with different `source` values. What they must not do is write the
same keys: give each source its own `subdir`, or sync different files.

### Limits

- Files over 16 MiB are skipped and reported, never uploaded: a larger file
  would go up as a multipart upload, which was never tested against
  SeaweedFS, and OpenViking refuses files over 8 MB anyway. A file that grows
  past the limit keeps its last copy in the bucket.
- The consumer decides what OpenViking does with a file: text is written as
  is, images get their own folder, other binaries are skipped.

## Config

`ov-sync-queue.toml` in the folder root, as `ovsq init` writes it:

```toml
[target]
bucket = "ov-sync"
endpoint = "https://s3.lab.orangecluster.nl"
region = "us-east-1"
prefix = "JasperHG90/notes"   # <owner>/<name>, required
subdir = ""                   # optional folder under the prefix
source = "local"              # names this folder's manifest

[sync]
include_folders = []          # e.g. ["docs", ".okf"]; empty means everything
include_extensions = [".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml", ".html", ".png", ".jpg", ".jpeg", ".gif", ".webp"]
exclude_folders = [".git", ".obsidian", ".trash", ".venv", "__pycache__", "node_modules"]
exclude = []                  # globs, e.g. "drafts/**", "*.tmp.md"
git_tracked_only = false      # true: only files git ls-files lists
```

Any key can be overridden with `OVSQ_<SECTION>__<KEY>`, for example
`OVSQ_TARGET__PREFIX=JasperHG90/notes` or
`OVSQ_SYNC__INCLUDE_FOLDERS='["docs"]'`. Every key, default and exit code is
in [the reference](../../docs/reference/ovsq-commands-and-config.md).

## Credentials

The standard chain, first match wins: `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN`; then `MINIO_ROOT_USER` and
`MINIO_ROOT_PASSWORD`; then the AWS shared credentials file (`AWS_PROFILE`
picks the profile). Short-lived STS credentials work through the first.

## Development

```console
$ ./check.sh          # gofmt, go vet, go test: the same gate CI and the loop run
$ ./e2e/run.sh        # against a real SeaweedFS in docker; not a gate
$ ./e2e/pipeline.sh   # LOCAL ONLY: the whole pipeline, see below
```

`e2e/pipeline.sh` runs ovsq through the whole pipeline in docker, as close
to production as a laptop gets. It needs a clone of localstack
(`LOCALSTACK_DIR`, default `../localstack` beside this repo), so it is local
only: CI has no clone, and it is not a gate.

- SeaweedFS and NATS run the images localstack pins. SeaweedFS runs the
  jobspec's own args, webhook config and `master.toml`, read from
  `seaweedfs.hcl`, inside the receiver's network namespace, so its webhook
  reaches the receiver on 127.0.0.1 as in production.
- The receiver and the consumer are the real `ov-sync-consumer` binary,
  built from the clone and run with the jobspec's environment.
- The consumer gets S3 credentials the production way: SeaweedFS's STS trades
  a Nomad-style workload token for the role `ov-sync-consumer.hcl` names,
  with `defaultEffect` Deny. SeaweedFS's `iam.json` is built as production
  builds it: `terraform console` evaluates `services.tf`'s own `iam_members`
  expression, and the result fills `seaweedfs.hcl`'s template. Only the
  OIDC issuer URL is swapped for the fake one. So the test needs terraform
  on PATH, and a change to the role, policy or trust in localstack shows up
  in the next run.
- Only OpenViking and the Nomad token issuer are faked (`e2e/pipeline/`).
  The catch-up pass is pushed out to 24h, so every change must arrive
  through an event.

It checks that text files and a PNG under a `subdir` reach OpenViking:
upload, edit, replace, and delete, with the image compared byte for byte.
It also checks that the consumer never touches ovsq's manifest, that
emptied folders are removed, and that no event fails or is dead-lettered.
The fake OpenViking keeps a deleted image's folder as an empty ghost, as
real OpenViking does, so the consumer's handling of that is tested too. `OVSQ_E2E_KEEP=1` leaves the containers up after a run.

The repo's `prek run --all-files` runs `check.sh` through the
`go-ov-sync-queue` hook, so the repo's gates now need Go on PATH.

Design notes: [architecture](../../.okf/architecture/ov-sync-queue.md) and
ADRs [0002](../../.okf/decisions/0002-ov-sync-queue-syncs-into-the-ov-sync-bucket-not-straight-to-the-queue.md),
[0003](../../.okf/decisions/0003-each-ovsq-source-owns-its-own-manifest-in-the-bucket.md)
and [0004](../../.okf/decisions/0004-ovsq-is-written-in-go.md).
