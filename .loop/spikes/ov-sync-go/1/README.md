---
type: spike
title: Spike ov-sync-go/1
description: does minio-go v7 do single-PUT uploads, manifest GETs (NoSuchKey when missing) and DELETEs against SeaweedFS S3 with a Read+Write, no-List identity?
tags:
- spike
generated:
  by: process:loopctl/2.3.1.dev65+g93b143856
  at: '2026-11-27T08:46:20+00:00'
slug: ov-sync-go
n: 1
outcome_written: true
---

# Spike ov-sync-go/1

does minio-go v7 do single-PUT uploads, manifest GETs (NoSuchKey when missing) and DELETEs against SeaweedFS S3 with a Read+Write, no-List identity?

The command exited 0:

```
/private/tmp/claude-502/-Users-jasperginn-Documents-code-projects-openviking-extensions/38afc982-80df-4d50-bb9c-7efed4cd88b4/scratchpad/spike-r1/run.sh
```

## Outcome

minio-go v7.4.1 against SeaweedFS (`chrislusf/seaweedfs:latest`, image built 2026-11-23, `server -s3`) passed all 9 steps with a static identity granted only `Read:ov-sync` and `Write:ov-sync`. Its ListObjects call was refused with AccessDenied, so the identity matched the CI policy.
The checks: a small PutObject, a 6 MB PutObject that read back byte-identical, a GetObject of the missing `_state/manifest.json` that returned `NoSuchKey`, a manifest put and get that matched, and a RemoveObject followed by a StatObject that returned `NoSuchKey`.
This rules out SeaweedFS refusing minio-go's default checksum headers or its PUT, GET and DELETE paths. A 6 MB object is below minio-go's 16 MiB multipart threshold, so it went up as one PUT, and multipart was not tested.
Settles R1. Limits: one SeaweedFS build, plain HTTP, and static v4 credentials instead of STS web-identity. Only the driver `run.sh` was saved as evidence; the Go program and `s3.json` it calls lived in the scratch directory.

## Files

- `run.json`: the command, when it ran, its exit, the commit and tree it ran on, and where its output went
- `stdout.txt`: its standard output
- `stderr.txt`: its standard error
- `files/run.sh`: a copy of `/private/tmp/claude-502/-Users-jasperginn-Documents-code-projects-openviking-extensions/38afc982-80df-4d50-bb9c-7efed4cd88b4/scratchpad/spike-r1/run.sh`, which the command used
