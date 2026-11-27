---
type: verdict
title: 'Verdict: ov-sync-go reviewer round 1'
description: 'reviewer verdict for round 1 of ov-sync-go: fail.'
tags:
- verdict
- review
generated:
  by: claude-opus/5.5
  at: '2026-11-27T09:13:51+00:00'
slug: ov-sync-go
pass: reviewer
round: 1
body_schema: verdict/1
---

## Summary

The tool follows the CI action's sync.py closely and the tests are honest, with one real delete-safety bug: running against a symlinked folder selects zero files, and `run --delete` then wipes the prefix. I reproduced it (see f1). Everything else is non-blocking.

## Checked

- `loopctl gate stamp` from the worktree: `stamp green: b0158192c2d66ac00583062738aa5a596ff035d5835078a7bbf37a4b5548612a`. lint, format and rows r1-r9 and r11 pass. The stamp did not list the new `go-ov-sync-queue` gate, apparently because it reads gates from the primary checkout's config, which does not have it yet. Row r9 runs vet and test anyway.
- `bash packages/ov-sync-queue/check.sh`: gofmt clean, vet clean, all packages pass.
- Read every changed Go file in full and compared syncer.go with the action's sync.py (fetched via gh). Uploads/deletes, keeping entries on failure, checkpoints only between batches, the final write only when the state changed, exit 1 on any failure: all match. Recording the hash of the bytes actually sent is better than the action.
- Eval rows: r1 checks keys, hashes, subdir paths, exact bytes and that manifest.json is absent. r2 checks no writes after both the no-change run and the cache loss. r3 checks no DELETE, the key kept, then removed and the entry dropped. r4 covers the failure cases and checkpoint sizes [2 4 5] (exit code only indirectly, f2). r5 covers _state, include folders, globs, extensions and git ls-files. r6 covers defaults, overrides and refusals naming the key. r7 compares the store log and the cache bytes, and checks counts against the run. r8 counts hasher calls. r11 checks the PUT, the skip line and the exit code.
- r10 (judgment): the README and the reference page cover every command, flag, config key, env var, the manifest and deletes. All config keys match config.go. The one mismatch is exit codes (f3).
- Reproduced f1 in a scratch copy of the module, using a test that symlinks the test folder and runs `run LINK --delete`. Output: `deleted JasperHG90/notes/a.md`, `deleted JasperHG90/notes/b.md`, exit 0, leaving only the manifest in the store.
- Checked subdir and prefix validation (`..`, `_state`, absolute paths), include_folders escape, and cache staleness on same size+mtime, which the plan accepts. Also checked concurrency (Prepare runs in order, each upload goroutine writes only its own slot, the fakestore locks) and flag parsing (`run DIR --delete`, `--`, extra positionals).

## Findings

- f1 (bug, blocking): `filepath.WalkDir(root)` Lstats its root, so a symlinked FOLDER (or a symlinked root reached through `filepath.Join(root, ".")`) is one non-dir entry and nothing is walked. Prepare then lists every manifest path as a delete. `status` shows the whole prefix as "would delete", and `run --delete` deletes it, and the cache is wiped too because Save keeps nothing. Fix: resolve the root with `filepath.EvalSymlinks` before scanning, or refuse a root that is not a directory. Add a test that a symlinked FOLDER syncs its files and deletes nothing. A guard that refuses `--delete` when zero files are selected but the manifest is non-empty would also be a cheap safety net.
- f2 (test-strength): add one CLI-level assertion that `ovsq run` returns 1 when an upload fails.
- f3 (docs): either make those Prepare errors exit 2 or say in docs/reference/ovsq-commands-and-config.md and the usage text that they exit 1.
- f4 (hardening): write the final manifest (and maybe the cache) with a context that ignores the interrupt.

## Notes

auth.md in the diff is trailing-whitespace churn only, from the prek hooks. It is harmless but has nothing to do with this ticket. When merging, keep main's prek-based ruff gates and add the go gate.
