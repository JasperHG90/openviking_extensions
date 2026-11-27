---
type: plan
title: 'Plan: ov-sync-go'
description: A Go CLI, ov-sync-queue, that mirrors a local folder into a prefix of the ov-sync bucket with the CI action's manifest logic, so ov-sync-consumer queues and writes it to OpenViking.
tags:
- plan
generated:
  by: process:loopctl/2.3.1.dev65+g93b143856
  at: '2026-11-27T08:43:51+00:00'
slug: ov-sync-go
epic: null
depends_on: []
priority: null
schema_version: '2.0'
---

# ov-sync-go

Relayed from: none

## Intent

Add a Go module at `new:packages/ov-sync-queue/go.mod` that builds one binary,
`ovsq`. It does for a local folder what the `openviking-sync` GitHub action
(JasperHG90/localstack, .github/actions/openviking-sync/sync.py) does for a
repo: it hashes the chosen files, compares them with a manifest object in the
`ov-sync` bucket, uploads what changed, deletes what the manifest lists and the
folder no longer has (only with `--delete`), and writes the manifest back after
every batch. It never talks to OpenViking. The bucket's webhook puts each
change on the JetStream stream `OV_SYNC`, and `ov-sync-consumer` writes it to
OpenViking. That pipeline is where documents get managed, paced and retried.

Choices made without the operator (they were asked and did not answer; each
is cheap to change later):

- Transport is the bucket, not a direct publish to JetStream. The consumer
  reads each object's content from S3, so a bare message carries nothing it
  can write.
- Each source owns its own manifest: <owner>/<name>/_state/ovsq-<source>.json,
  where `source` is a config key (default `local`, letters, digits, dashes).
  It is never `manifest.json`, which the CI action owns, so neither tool
  deletes the other's files. The consumer skips every key under
  `<owner>/<name>/_state/`, so manifests never reach OpenViking. The format is
  the action's: path to sha256, where the path is relative to the prefix and
  already includes `subdir`, so a delete targets exactly `<prefix>/<path>`.
  Two sources writing the same key is the operator's mistake to avoid; the
  README says so.
- A local cache, `.ov-sync-queue.db` beside the folder like
  `packages/ov-sync/src/ov_sync/state.py`, keeps size, mtime and hash so an
  unchanged file is never re-read. The bucket manifest stays the record of
  truth: losing the cache costs re-hashing, never re-uploading or a missed
  delete.
- S3 credentials come from minio-go's standard chain (env vars including a
  session token, an AWS profile), the client `ov-sync-consumer` already uses
  against SeaweedFS. Built-in Vault or STS exchange is out of scope.
- First version: `ovsq init`, `ovsq status`, `ovsq run`. `watch` is a later
  ticket.

The config, `ov-sync-queue.toml` in the folder root, decides what is synced
(`include_folders`, `include_extensions`, exclude globs, an optional
`git_tracked_only` that lists files with `git ls-files` as the action does)
and where it lands (`bucket`, `endpoint`, `region`, `prefix` as
`<owner>/<name>` which the consumer maps to a `viking://` folder, an optional
`subdir` under it, and `source`). Every key can be overridden by an `OVSQ_`
environment variable.

Outside this repo, and not part of this ticket: for files to reach OpenViking,
the operator adds the prefix to the consumer's prefix map
(deployments/applications/services.tf in localstack) and gives the laptop a
write credential for that prefix (the bucket has `writers = []` today). The
README names both steps. This ticket delivers the tool up to the bucket.

The outcome that shows it worked: against a fake S3 server in tests, a first
`ovsq run` uploads every selected file and writes its manifest, and a second
run with no change uploads nothing.

## Problems

- P1: ov-sync writes straight to OpenViking, so a local folder has no way into the bucket that feeds the queue which paces, retries and dead-letters writes.
- P2: There is no way to choose which local files go to the bucket and under which prefix, short of copying `sync.py` and its folder list.
- P3: Nothing records which local files were sent to the bucket, so deletes cannot be mirrored and unchanged files would be re-sent.
- P4: The repo's gates cover Python and TypeScript only, so a Go module would ship unchecked.

## Goals

- G1: A local folder can be mirrored into a prefix of the ov-sync bucket with the CI action's manifest logic, so the queue and consumer can carry it to OpenViking.
- G2: A config file decides what is synced and where in the bucket it lands.

## Outcomes

### O1: `ovsq run` mirrors a folder into a bucket prefix using the action's manifest logic
- kind: behaviour
- goal: G1
- fixes: P1
- needs: O2, O3
- touches: `new:packages/ov-sync-queue/main.go`, `ovsq run`
- checked by: Go tests against an in-process fake S3 (upload, unchanged second run, `--delete`, failed upload keeps its old manifest entry, manifest written after each batch, files under `_state/` never uploaded, `manifest.json` never written)
- seen by: operator

### O2: `ov-sync-queue.toml` selects files and the target prefix
- kind: support
- goal: G2
- fixes: P2
- needs: none
- touches: `ov-sync-queue.toml`, `ovsq init`
- checked by: Go tests for loading, defaults, `OVSQ_` overrides, and refusing a prefix that is not `<owner>/<name>` or a bad `source`
- seen by: operator

### O3: A local cache and a per-source bucket manifest record what was sent
- kind: support
- goal: G1
- fixes: P3
- needs: none
- touches: `.ov-sync-queue.db`, `ovsq status`
- checked by: Go tests showing a file with matching size and mtime is not re-hashed, a lost cache costs no re-upload when the manifest matches, and `status` makes no write
- seen by: operator

### O4: The Go gates run with the repo's other gates, and the package is documented
- kind: support
- goal: G1
- fixes: P4
- needs: O1
- touches: `.pre-commit-config.yaml`, `.loop/config.toml`, `new:packages/ov-sync-queue/README.md`
- checked by: `prek run --all-files` and `loopctl gate stamp` run `go vet` and `go test` for the module
- seen by: calling code

## Could invalidate this plan

- R1: minio-go may not do single-PUT uploads, GETs of the manifest and DELETEs against SeaweedFS's S3 the way the plan assumes (for example, a checksum header or multipart path SeaweedFS refuses), which would force a different client. Settled by spike:ov-sync-go/1:
  minio-go v7.4.1 passed small and 6 MB single PUTs, a NoSuchKey GET of a
  missing manifest, a manifest round trip, and a DELETE against SeaweedFS with
  a Read+Write, no-List identity. Multipart was not tested, so `ovsq` skips and
  reports files over 16 MiB (minio-go's multipart threshold) instead of
  uploading them; OpenViking's 8 MB per-file limit makes such files useless
  downstream anyway.
