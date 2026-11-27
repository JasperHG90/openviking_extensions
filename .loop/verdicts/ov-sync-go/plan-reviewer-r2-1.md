---
type: verdict
title: 'Verdict: ov-sync-go plan-reviewer round 2'
description: 'plan-reviewer verdict for round 2 of ov-sync-go: pass.'
tags:
- verdict
- plan
generated:
  by: claude-opus/5.5
  at: '2026-11-27T08:50:58+00:00'
slug: ov-sync-go
pass: plan-reviewer
round: 2
body_schema: verdict/1
---

## Summary

All three round 1 findings are fixed, and nothing new would break the plan. The plan now promises delivery into the bucket and names the two localstack steps (prefix map, write credential) as the operator's work outside this ticket. Each source owns its own manifest key under `_state/`, which the consumer skips. O4 now fixes P4. R1 is still settled by spike ov-sync-go/1.

## Checked

- Round 1 verdict, plan-reviewer-r1-1, and each finding against the revised plan.
- Spike ov-sync-go/1: README outcome, stdout.txt (9 PASS, ListObjects denied), empty stderr.txt, run.json exit 0. It settles R1 for single PUT, NoSuchKey GET, manifest round trip and DELETE. Multipart is avoided by the 16 MiB skip.
- localstack `mapping.go`: `Resolve` takes `<owner>/<repo>/` as the first two key segments and skips `rest == "_state"` and `_state/...`. So `<prefix>/_state/ovsq-<source>.json` never reaches OpenViking, and a `<prefix>/<subdir>/<path>` key maps to `root + subdir/path` as the plan assumes. `LoadMapping` requires exactly two segments, which matches the plan's `<owner>/<name>` prefix check.
- localstack `sync.py`: deletes are `[p for p in old if p not in new]` over its own `_state/manifest.json`. With a separate ovsq manifest, CI deletes only keys it uploaded.
- 2.0 facts: P1 to P4 each match a cause in the prose, and O1 to O4 match the prose's commands (`run`, `init`/config, `status`/cache, gates and README). Kinds, needs and fixes are consistent. O4 needing O1 is right, because gates need a module to run on.
- This repo: `.pre-commit-config.yaml` and `.loop/config.toml` have no Go gate today, which confirms P4.

## Findings

p1 resolved: the out-of-repo steps are now named and out of scope, and G1/P1 match what this repo can deliver.
p2 resolved: one manifest per source, never `manifest.json`, with subdir inside manifest paths.
p3 resolved: O4 fixes P4.

## Notes

These are for the build and do not block. The 16 MiB skip appears only under R1, and no outcome's `checked by` or eval row tests it, so the builder should add a test. Adding `go vet`/`go test` to `.loop/config.toml` gates makes every ticket's stamp need a Go toolchain. That is fine on this machine, but the README should say so. r9 greps only the pre-commit config, not the loop gates the outcome names.
