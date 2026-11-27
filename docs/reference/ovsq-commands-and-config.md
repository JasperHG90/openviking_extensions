---
description: "Every ovsq command, flag, config key, environment variable and exit code."
stale_after: 2027-02-27
sources:
  - resource: packages/ov-sync-queue/main.go
  - resource: packages/ov-sync-queue/internal/config/config.go
  - resource: packages/ov-sync-queue/internal/scan/scan.go
  - resource: packages/ov-sync-queue/internal/syncer/syncer.go
  - resource: packages/ov-sync-queue/internal/s3store/s3store.go
---

# ovsq commands and config

## Summary

`ovsq` (package `packages/ov-sync-queue`) mirrors a local folder into one
prefix of the `ov-sync` S3 bucket. The bucket's webhook queues each change for
`ov-sync-consumer`, which writes it to OpenViking. `ovsq` itself never talks
to OpenViking.

## Commands

| Command | What it does |
|---|---|
| `ovsq init [FOLDER]` | Writes a starter `ov-sync-queue.toml`. Refuses when one exists. |
| `ovsq status [FOLDER]` | Prints what the next run would upload and delete. Writes nothing, not even the local cache. |
| `ovsq version` | Prints `ovsq` and its version: the release's, or `dev` for a source build. `--version` does the same. |
| `ovsq run [FOLDER] [--delete]` | Uploads new and changed files and writes the manifest. Without `--delete`, it reports files gone from the folder; with it, it removes their keys. When the scan selects no files at all, `--delete` removes nothing and the run exits 1, since that is likelier a wrong folder than an emptied one. |

`FOLDER` defaults to the current folder and may be a symlink. Flags may come before or after it.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every file synced (skipped large files do not count as failures). |
| 1 | An upload, delete, hash or manifest write failed (re-run to retry exactly those), or the run could not start: the folder or an `include_folders` entry is missing, `git ls-files` failed, the manifest could not be read, or `--delete` was refused because no files were selected. |
| 2 | A usage or config file error: unknown command, bad or unknown key, missing `ov-sync-queue.toml`. |

## Config keys

`ov-sync-queue.toml` in the folder root. An unknown key is an error.

| Key | Default | Meaning |
|---|---|---|
| `target.bucket` | `"ov-sync"` | The bucket. |
| `target.endpoint` | `"https://s3.lab.orangecluster.nl"` | S3 endpoint, with `http://` or `https://`. |
| `target.region` | `"us-east-1"` | Sent with every request. |
| `target.prefix` | none, required | `<owner>/<name>`: the two-part key ov-sync-consumer maps to a `viking://` folder. |
| `target.subdir` | `""` | Folder under the prefix that files land in. May not be under `_state/`. |
| `target.source` | `"local"` | Letters, digits and dashes. Names the manifest `<prefix>/_state/ovsq-<source>.json`. |
| `sync.include_folders` | `[]` | Folders under the root to sync; empty means all. A missing one is an error. |
| `sync.include_extensions` | `.md .txt .csv .json .yaml .yml .toml .html .png .jpg .jpeg .gif .webp` | Extensions to sync, case-insensitive; empty means all. |
| `sync.exclude_folders` | `.git .obsidian .trash .venv __pycache__ node_modules` | Folder names skipped at any depth. |
| `sync.exclude` | `[]` | Globs relative to the root; `**` crosses folders. |
| `sync.git_tracked_only` | `false` | Only files `git ls-files` lists, as the CI action does. |

Never synced, whatever the config says: `ov-sync-queue.toml`,
`.ov-sync-queue.db`, anything under a top-level `_state/`, symlinks, and files
over 16 MiB (reported as `skipped`).

## Environment variables

| Variable | Meaning |
|---|---|
| `OVSQ_<SECTION>__<KEY>` | Overrides a config key, e.g. `OVSQ_TARGET__PREFIX`. Lists take a JSON array, booleans `true` or `false`. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | S3 credentials, tried first. |
| `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` | Tried second. |
| `AWS_PROFILE`, `AWS_SHARED_CREDENTIALS_FILE` | The shared credentials file, tried last. |

## Bucket layout

| Key | Holds |
|---|---|
| `<prefix>/[<subdir>/]<path>` | A synced file, byte for byte. |
| `<prefix>/_state/ovsq-<source>.json` | This source's manifest: path (relative to the prefix, subdir included) to sha256, sorted, one-space indent. |
| `<prefix>/_state/manifest.json` | The CI action's manifest. `ovsq` never reads or writes it. |
