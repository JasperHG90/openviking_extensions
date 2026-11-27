---
type: summary
title: "Summary"
description: "The whole project in at most 500 words: what the knowledge says, citing the concepts; start here."
generated:
  by: process:kmctl/0.18.0
  at: 2026-11-27T06:51:37Z
tags: [summary]
---

# Summary

## The project

Extensions around OpenViking, one package per folder under `packages/`: a
pgvector store, server extensions, CLIs, agent skills, a dashboard, a browser
clipper and two folder syncs. The manifesto is not written yet, so the
project's purpose and values are only in the root README for now.

## How it works

The bundle covers one design so far: `ovsq`
([architecture](architecture/ov-sync-queue.md)), which mirrors a local folder
into a prefix of the `ov-sync` bucket. It picks files from its config, hashes
them with a local cache, compares them with its manifest in the bucket, and
uploads and deletes to match. Everything after the bucket (webhook, JetStream
queue, `ov-sync-consumer`, OpenViking) lives in JasperHG90/localstack.

## What it has settled

- Knowledge lives in this OKF bundle next to the code
  ([ADR 0001](decisions/0001-keep-project-knowledge-in-an-okf-bundle.md)).
- Local documents should reach OpenViking through the queue that paces and
  retries writes, not straight from a laptop, so `ovsq` stops at the bucket
  ([ADR 0002](decisions/0002-ov-sync-queue-syncs-into-the-ov-sync-bucket-not-straight-to-the-queue.md)).
- Whoever writes a set of keys owns their record: each `ovsq` source keeps
  its own manifest beside the CI action's, and the bucket copy, not the local
  cache, is the truth
  ([ADR 0003](decisions/0003-each-ovsq-source-owns-its-own-manifest-in-the-bucket.md)).
- `ovsq` is Go, matching the consumer it feeds, so the gates need a Go
  toolchain ([ADR 0004](decisions/0004-ovsq-is-written-in-go.md)).

## Where it stands

`ovsq` has `init`, `status`, `run` and `version`, and the release workflow
ships it as stamped static binaries
([ADR 0005](decisions/0005-ship-ovsq-as-stamped-static-binaries-from-the-release-workflow.md));
`watch` is not built. Reaching
OpenViking uses localstack's one `JasperHG90/local` prefix and 15-minute
credentials from `localstack token s3 local-ov-sync`, both outside this
repo. The other packages have no architecture pages or decision records yet.
