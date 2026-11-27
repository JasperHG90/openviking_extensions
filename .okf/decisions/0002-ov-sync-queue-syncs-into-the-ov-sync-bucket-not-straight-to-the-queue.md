---
type: decision
title: "ADR 0002: ov-sync-queue syncs into the ov-sync bucket, not straight to the queue"
description: "ovsq uploads files to the ov-sync S3 bucket, whose webhook queues them for ov-sync-consumer, instead of publishing to JetStream or writing to OpenViking."
tags: [decision]
generated:
  by: process:kmctl/0.18.0
  at: 2026-11-27T08:59:35Z
status: stable
sources:
  - resource: "packages/ov-sync-queue/internal/syncer/syncer.go"
  - resource: "packages/ov-sync-queue/internal/s3store/s3store.go"
---

# ADR 0002: ov-sync-queue syncs into the ov-sync bucket, not straight to the queue

## Context

ov-sync (`packages/ov-sync`) writes a local folder straight to OpenViking.
Every write makes OpenViking re-embed and re-summarise, with no pacing, no
retry that outlasts an outage, and no dead letter. In JasperHG90/localstack,
CI repos already reach OpenViking a better way: the `openviking-sync` action
uploads files to the SeaweedFS bucket `ov-sync`, the bucket's webhook puts each
change on the JetStream stream `OV_SYNC`, and `ov-sync-consumer` writes them
to OpenViking behind a worker pool, a rate limit, retries and a dead-letter
subject. The operator asked for a local tool that goes through that pipeline
("a P/S queue, not directly to OV").

## Decision

`ovsq` uploads files to the bucket, with one PUT per file, and never talks to
JetStream or OpenViking. The consumer reads each object's current content from
S3 when it handles an event, so the bucket is the only input it can act on.

Rejected:

- **Publishing events to `OV_SYNC` directly.** The consumer reads the object
  from S3 for every event, so a message whose object is not in the bucket
  writes nothing. Carrying content in the message would need a new message
  shape and consumer changes in another repo, and a second credential (NATS)
  on every laptop.
- **Uploading and also publishing.** The webhook already publishes every
  bucket write, with a dedupe id, so a second publish would only add load.

## Consequences

- Files reach OpenViking only for a prefix in the consumer's prefix map
  (`deployments/applications/services.tf` in localstack), and only with a
  credential allowed to write that prefix. Both live outside this repo, and
  the package README names them.
- The pipeline paces, retries and dead-letters every local change exactly as
  it does CI's.
- minio-go is the client, as in the consumer. Spike ov-sync-go/1 showed it
  works against SeaweedFS with a read-and-write, no-list identity. Multipart
  uploads were not tested, so ovsq skips files over 16 MiB.
