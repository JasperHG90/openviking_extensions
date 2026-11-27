---
type: decision
title: "ADR 0004: ovsq is written in Go"
description: "The bucket sync is a Go module in packages/, unlike the Python ov-sync, matching ov-sync-consumer and its minio-go client."
tags: [decision]
generated:
  by: process:kmctl/0.18.0
  at: 2026-11-27T08:59:35Z
status: stable
sources:
  - resource: "packages/ov-sync-queue/go.mod"
---

# ADR 0004: ovsq is written in Go

## Context

Every other sync tool in this repo is Python (`packages/ov-sync`). The
operator asked for this one in Go. The pipeline it feeds,
`ov-sync-consumer`, is Go and uses minio-go against the same SeaweedFS.

## Decision

`packages/ov-sync-queue` is a Go module that builds one static binary. It
uses minio-go (as the consumer does), BurntSushi/toml for the config, bbolt
for the local cache and doublestar for exclude globs.

Rejected: **extending the Python ov-sync with a bucket target.** It would
reuse ov-sync's scanner and config, but the operator asked for Go, and the two
tools share little beyond file selection: ov-sync's batching, URI rewriting and
snapshots are all about OpenViking's API, which ovsq never calls.

## Consequences

- The repo's gates need a Go toolchain: `packages/ov-sync-queue/check.sh` runs
  gofmt, go vet and go test from both the pre-commit hook and the loop gate.
- File selection is written twice, once in each language, and the two can
  drift. ovsq's config keys follow ov-sync's names where they mean the same.
