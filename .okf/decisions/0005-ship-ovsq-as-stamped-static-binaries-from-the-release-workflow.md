---
type: decision
title: "ADR 0005: Ship ovsq as stamped static binaries from the release workflow"
description: "The release workflow cross-builds one static ovsq binary per platform, stamps the version with -ldflags, and attaches them with SHA256SUMS, instead of goreleaser or go install."
tags: [decision]
generated:
  by: claude-opus/5.5
  at: 2026-11-27T14:51:12Z
status: stable
sources:
  - resource: ".github/workflows/release.yaml"
  - resource: "packages/ov-sync-queue/main.go"
---

# ADR 0005: Ship ovsq as stamped static binaries from the release workflow

## Context

`ovsq` runs on laptops, macOS and Linux, on Intel and ARM. Every other
package releases through the manual `release.yaml` workflow, where the git
tag is the only place a version lives. A Go binary has no metadata for
hatch-vcs to fill, so the version must be put in at build time.

## Decision

The publish job in `release.yaml` builds `ovsq` for darwin and linux,
amd64 and arm64, with `CGO_ENABLED=0 go build -trimpath -ldflags "-X
main.version=<version>"`. It attaches one `.tar.gz` per platform and a
`SHA256SUMS`, and reads the version back from the linux-amd64 build with
`ovsq version` before publishing. Its gate is a `test-go` job that runs
`check.sh`, the script CI and the loop already run.

Rejected:

- **goreleaser.** It does the same job but adds a tool and a config file
  for four `go build` calls, and it wants to own the tag and the GitHub
  release, which `release.yaml` already does for every package.
- **`go install` only.** It needs a Go toolchain on every laptop, and the
  binary reports no version.
- **A build matrix job.** Pure Go cross-compiles in seconds without
  emulation, so separate runners would only add an artifact hand-off.

## Consequences

- Installing needs only `curl` and `tar`. A source build reports `dev`.
- A platform not in the list needs a source build.
- The asset names (`ovsq-<version>-<os>-<arch>.tar.gz`) are an interface:
  the how-to and the README download them by name.
