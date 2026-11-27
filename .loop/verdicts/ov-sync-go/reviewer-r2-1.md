---
type: verdict
title: 'Verdict: ov-sync-go reviewer round 2'
description: 'reviewer verdict for round 2 of ov-sync-go: fix.'
tags:
- verdict
- review
generated:
  by: claude-opus/5.5
  at: '2026-11-27T09:18:29+00:00'
slug: ov-sync-go
pass: reviewer
round: 2
body_schema: verdict/1
---

## Summary

All four findings are fixed, and the fixes break nothing. The symlinked-folder delete bug is closed twice: the scan now resolves the root, and a guard refuses `--delete` when the scan selected nothing but the manifest still lists files. One small new docs gap is left (f5): the CLI usage text does not mention the refused `--delete`. It does not block.

## Checked

- `loopctl gate stamp` from the worktree: `stamp green: 5136f734bac7ba8fb97608c5bfc9f54ab08e2677b0d851d77b12245b4bfdae24`. lint, format and rows r1-r9 and r11 pass.
- `bash packages/ov-sync-queue/check.sh`: all packages pass.
- scan.Select: EvalSymlinks then Stat+IsDir, and the error names the original path. Engine.Root keeps the unresolved path, and that is fine: Hash and ReadFile follow the link, and Rel comes from the resolved root. Include folders are still Lstat'ed under the resolved root, so their escape check is unchanged.
- syncer.Run guard: `del && len(p.selected)==0 && len(p.Deletes)>0`. p.selected also counts skipped and unreadable files, so a folder that holds only oversized files does not trip it. The guard adds a failure, so the run exits 1, and it sets del=false, so the summary reports 0 to delete and nothing is removed from the manifest. Uploads cannot happen when nothing is selected, so this path writes no manifest.
- TestDeleteSafety, symlink subtest: it needs exit 0 and zero writes through the link. The guard alone would exit 1 and fail it, so the test really covers the scan fix. Refusal subtest: it checks exit 1, the "refusing" text and no DELETE.
- TestSelection now has subtests for a symlinked root and for a root that is a file or missing.
- TestRunFailuresKeepManifestTrue (main_test.go) checks exit 1 and the old hash kept. Row r4's `./...` regex runs it next to the syncer test with the same name.
- TestManifestWriteSurvivesCancel: cancelStore makes the manifest Put fail on a cancelled ctx, so without WithoutCancel the test would fail. Both the checkpoint and final writes use keepCtx. Uploads and deletes still respect cancellation.
- The docs reference page's exit-code table and the README now match the code paths (main.go returns 1 at the Prepare and run failures, 2 for usage and config errors).

## Findings

- f1 resolved: scan.go:37-44 resolves and checks the root, and syncer.go:214-220 adds the refusal guard. Tests: TestSelection symlink and bad-root subtests, and both TestDeleteSafety subtests.
- f2 resolved: main_test.go:305 asserts exit 1 from `ovsq run` and that the manifest entry is kept.
- f3 resolved: the reference page's exit-code table and the usage text cover unreadable folder, include folder, git and manifest as exit 1.
- f4 resolved: syncer.go:189 uses context.WithoutCancel for both manifest writes, tested by TestManifestWriteSurvivesCancel.
- f5 open (docs, non-blocking): the usage text at main.go:40-42 lists the exit-1 causes but leaves out the `--delete` refused because nothing was selected. The implementer's brief says it covers it. Add "or --delete was refused because no files were selected".
