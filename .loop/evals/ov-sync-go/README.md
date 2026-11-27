---
type: eval
title: 'Eval: ov-sync-go'
description: 'Definition of done for ov-sync-go: 11 row(s).'
tags:
- eval
generated:
  by: process:loopctl/2.3.1.dev65+g93b143856
  at: '2026-11-27T08:51:19+00:00'
slug: ov-sync-go
signoff:
  signer: delegated:jasperginn@gmail.com/claude
  hash: 5385239912387e2677da07e5a2d1c5060c21ea6839beb024b77879188053c7ef
  at: '2026-11-27T08:51:19+00:00'
verified: null
---

Rows: rows.json; the fields each row takes: loopctl schema eval-rows.

- r1: A first ovsq run uploads every selected file to <prefix>/<path> and writes <prefix>/_state/ovsq-<source>.json mapping each prefix-relative path (subdir included) to its sha256, in the CI action's format, and never writes <prefix>/_state/manifest.json.
  Fails when: a selected file is not in the fake bucket under its key, the manifest is missing, not path-to-sha256 JSON, lists a wrong hash or a path without subdir, or manifest.json is written
- r2: A second run with no change, and a run after the local cache is deleted while the manifest matches, uploads nothing.
  Fails when: the fake bucket records any PUT other than none on either run
- r3: A file removed from the folder is reported but not deleted without --delete, and with --delete its key is removed and its manifest entry dropped.
  Fails when: a run without --delete issues a DELETE, or a run with --delete leaves the key or its manifest entry
- r4: A failed upload keeps the file's old manifest entry and makes ovsq run exit non-zero, a failed delete keeps its entry, and the manifest is written after every batch so a run killed midway keeps its progress.
  Fails when: the manifest records a hash for a failed upload, drops a failed delete, the run exits 0, or a mid-run manifest is not written after a full batch
- r5: Files under _state/ in the folder, files outside include_folders, excluded globs and wrong extensions are never uploaded; git_tracked_only selects files by git ls-files.
  Fails when: any such file is uploaded, or an untracked file is uploaded with git_tracked_only on
- r6: The config loads from ov-sync-queue.toml with defaults, OVSQ_ environment variables override it, and a prefix that is not <owner>/<name> or a source that is not letters, digits and dashes is refused with a message naming the key.
  Fails when: a default, an override or the refusal does not behave as stated
- r7: ovsq status prints what the next run would upload and delete and makes no write to the bucket or the local cache.
  Fails when: status issues a PUT or DELETE, changes the cache file, or its counts differ from what run then does
- r8: A file whose size and mtime match the local cache is not read again.
  Fails when: the hasher is called for an unchanged file
- r11: A selected file over 16 MiB is skipped and reported, never uploaded, and its absence does not fail the run.
  Fails when: the file is PUT, the run prints no skip line naming it, or the run exits non-zero because of it
- r9: The module builds, vets and passes its whole test suite, and the repo's gates run it.
  Fails when: go vet or go test fails, or neither the pre-commit config nor the loop gates run them
- r10 (judgment): The package README explains install, config keys, how files reach OpenViking through the bucket and queue, the manifest, and deletes, and docs/ gains a reference page for the config.
  Fails when: a config key or command is undocumented, or the docs describe behavior the code does not have
