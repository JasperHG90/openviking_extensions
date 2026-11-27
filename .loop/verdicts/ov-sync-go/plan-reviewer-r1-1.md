---
type: verdict
title: 'Verdict: ov-sync-go plan-reviewer round 1'
description: 'plan-reviewer verdict for round 1 of ov-sync-go: fail.'
tags:
- verdict
- plan
generated:
  by: claude-opus/5.5
  at: '2026-11-27T08:48:43+00:00'
slug: ov-sync-go
pass: plan-reviewer
round: 1
body_schema: verdict/1
---

## Summary

The client approach holds up. Spike ov-sync-go/1 shows minio-go doing single PUTs, a NoSuchKey manifest GET, a manifest round trip and a DELETE against SeaweedFS with a Read+Write, no-List identity, which settles R1. Skipping files over 16 MiB avoids the multipart path the spike did not test. Two premises break the plan's goal, though. A new prefix reaches OpenViking only after changes in localstack (a prefix-map entry and a write credential), and the plan never says so. Sharing a prefix with CI, which the plan presents as a feature, gets files deleted, because the action's run always mirrors deletes. One outcome fact (O4 fixes P1) also does not match the prose.

## Checked

- Spike ov-sync-go/1: README outcome, stdout.txt (all 9 steps PASS, ListObjects denied), empty stderr.txt, run.json (exit 0). It answers R1 within its stated limits (static v4 creds, plain HTTP, one SeaweedFS build).
- The action's sync.py and action.yml in JasperHG90/localstack: manifest format (path to sha256, `json.dumps(indent=1, sort_keys=True)`), batches of 100, a checkpoint write after each batch, failed upload or delete keeps its entry, deletes always on, credentials from Vault to STS per `ci-<role>`.
- ov-sync-consumer: the component doc and `internal/consumer/mapping.go`. `Resolve` skips unmapped prefixes and `_state/`. The prefix map in `deployments/applications/services.tf` has two entries. The `ov-sync` bucket in `storage.tf` has `writers = []`.
- This repo: `packages/` (no Go module yet), `.pre-commit-config.yaml` (per-package hooks, no Go hook), `.loop/config.toml` (ruff gates only). Go 1.26.5 is installed locally.

## Findings

- p1 (premise): the end-to-end path the goal promises depends on a prefix-map entry and a credential that live in localstack. The plan does not list either as a dependency or as a risk the operator accepts.
- p2 (design): the plan says CI and a laptop can share a prefix, but the action's unconditional deletes make sharing destructive. Several `subdir` sources under one prefix share one manifest. The plan does not say how `subdir` maps to keys and manifest paths.
- p3 (premise): O4's `fixes: P1` names a problem that O4 does not fix.

## Notes

The STS web-identity path a CI runner would use with ovsq was not exercised by the spike, which used static credentials. If the plan keeps "CI can run ovsq after its Vault to STS step" as a goal, a short spike through minio-go's credential chain with session tokens would settle it. The ticket is not too big to split. Once p1 is placed in scope, one ticket still fits.
