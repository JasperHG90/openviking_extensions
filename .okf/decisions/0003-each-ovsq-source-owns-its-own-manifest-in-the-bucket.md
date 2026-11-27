---
type: decision
title: "ADR 0003: Each ovsq source owns its own manifest in the bucket"
description: "ovsq keeps the CI action manifest format but writes it to <prefix>/_state/ovsq-<source>.json, with a local cache only to skip re-hashing."
tags: [decision]
generated:
  by: process:kmctl/0.18.0
  at: 2026-11-27T08:59:35Z
status: stable
sources:
  - resource: "packages/ov-sync-queue/internal/syncer/syncer.go"
  - resource: "packages/ov-sync-queue/internal/state/state.go"
  - resource: "packages/ov-sync-queue/internal/config/config.go"
---

# ADR 0003: Each ovsq source owns its own manifest in the bucket

## Context

ov-sync keeps its record of what it sent in a local SQLite file. The CI
action keeps its record in the bucket, at `<owner>/<repo>/_state/manifest.json`,
mapping each path to its sha256, and deletes any key it lists whose file is
gone. A laptop and CI may sync into one prefix, and two local folders may too.
If they shared one manifest, each would delete the other's files.

## Decision

`ovsq` writes the action's manifest format (sorted path-to-sha256 JSON,
one-space indent) to `<prefix>/_state/ovsq-<source>.json`, where `source` is a
config key that defaults to `local`. It never touches `manifest.json`. Manifest
paths are relative to the prefix and include `subdir`, so a delete targets
exactly `<prefix>/<path>`. The consumer skips every key under
`<prefix>/_state/`, so manifests never reach OpenViking.

A local cache, `.ov-sync-queue.db` (bbolt), stores size, mtime and hash so an
unchanged file is never read. The bucket manifest stays the record of truth.

Rejected:

- **Sharing `manifest.json` with the CI action.** CI deletes every entry that
  `git ls-files` does not list, so it would delete a laptop's uploads.
- **Local state only, as ov-sync does.** Two machines syncing one source would
  disagree about what to delete, and a lost state file would leave deleted
  files in the bucket for good, since the credential cannot list it.

## Consequences

- Losing the local cache costs re-hashing, never a re-upload or a missed
  delete.
- Two sources writing the same key overwrite each other, and either may later
  delete it. The README tells the operator to keep sources apart, with
  `subdir` or separate folders.
- A failed upload keeps its old entry and a failed delete keeps its entry, so
  the next run retries exactly those, as in the action.
