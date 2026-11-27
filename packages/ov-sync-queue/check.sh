#!/usr/bin/env bash
# The Go gates for ov-sync-queue: gofmt, go vet and the tests. Run by the
# pre-commit hook and the loop gate, from any folder.
set -euo pipefail
cd "$(dirname "$0")"
unformatted=$(gofmt -l .)
if [ -n "$unformatted" ]; then
    echo "gofmt needed on: $unformatted" >&2
    exit 1
fi
go vet ./...
go test ./... -count=1
