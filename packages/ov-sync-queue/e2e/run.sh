#!/usr/bin/env bash
# Run ovsq against a real SeaweedFS S3 in a container, with an identity that
# may read and write the ov-sync bucket but not list it, as the CI credential.
# Needs docker (or podman as docker) and Go. Not part of the gates.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
name=ovsq-e2e-$$
port=${OVSQ_E2E_PORT:-18334}
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT

(cd "$here/.." && go build -o "$work/ovsq" .)
docker run -d --name "$name" -p "$port:8333" -v "$here/s3.json:/etc/s3.json:ro" \
    chrislusf/seaweedfs:latest server -s3 -s3.config=/etc/s3.json >/dev/null
for _ in $(seq 60); do curl -s -o /dev/null "http://127.0.0.1:$port/" && break; sleep 1; done
sleep 3
echo "s3.bucket.create -name ov-sync" | docker exec -i "$name" weed shell >/dev/null

mkdir -p "$work/notes/docs" && cd "$work/notes"
echo "# hello" > docs/a.md
echo "b" > b.md
head -c 20000000 /dev/zero > big.md
"$work/ovsq" init . >/dev/null
sed -i.bak -e 's#^prefix = ""#prefix = "JasperHG90/e2e"#' \
    -e "s#^endpoint = .*#endpoint = \"http://127.0.0.1:$port\"#" ov-sync-queue.toml
rm ov-sync-queue.toml.bak
export AWS_ACCESS_KEY_ID=ovsqkey AWS_SECRET_ACCESS_KEY=ovsqsecret

expect() { # expect LINE OUTPUT
    grep -qF "$1" <<<"$2" || { echo "FAIL: missing '$1' in:"; echo "$2"; exit 1; }
}
out=$("$work/ovsq" status .); expect "2 to upload, 0 to delete, 0 unchanged" "$out"
out=$("$work/ovsq" run .); expect "2 to upload, 0 to delete, 0 failed" "$out"; expect "skipped big.md" "$out"
out=$("$work/ovsq" run .); expect "0 to upload, 0 to delete, 0 failed" "$out"
rm .ov-sync-queue.db
out=$("$work/ovsq" run .); expect "0 to upload, 0 to delete, 0 failed" "$out"
rm b.md; echo changed > docs/a.md
out=$("$work/ovsq" run . --delete); expect "1 to upload, 1 to delete, 0 failed" "$out"
out=$("$work/ovsq" status .); expect "0 to upload, 0 to delete, 1 unchanged" "$out"
echo "PASS: ovsq against SeaweedFS $(docker image inspect chrislusf/seaweedfs:latest --format '{{.Created}}')"
