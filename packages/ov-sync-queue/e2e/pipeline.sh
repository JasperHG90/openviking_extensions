#!/usr/bin/env bash
# LOCAL ONLY: not in CI and not a gate. It needs docker, Go and a clone of
# JasperHG90/localstack (LOCALSTACK_DIR, default ../localstack beside this
# repo), because it builds ov-sync-consumer from that clone.
#
# Runs ovsq through the whole ov-sync pipeline, each piece in a container on
# one network:
#
#   ovsq -> SeaweedFS (filer webhook) -> receiver -> NATS JetStream (OV_SYNC)
#        -> ov-sync-consumer -> fake OpenViking
#
# SeaweedFS and NATS use the images localstack pins. The receiver is the real
# binary. The consumer is the real consumer package, started by
# pipeline/main.go with static S3 keys in place of the Nomad STS exchange and
# without the catch-up pass, so every file that reaches the fake OpenViking
# got there through an event.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
localstack=${LOCALSTACK_DIR:-$(dirname "$repo")/localstack}
app=$localstack/applications/ov-sync-consumer
[ -f "$app/go.mod" ] || { echo "no ov-sync-consumer at $app: set LOCALSTACK_DIR" >&2; exit 2; }

pin() { # pin FILE: the image the jobspec runs
    grep -Eo 'docker\.io/[a-z0-9/._-]+:[A-Za-z0-9._-]+' "$localstack/deployments/infrastructure/services/$1" | head -1
}
seaweed_image=$(pin seaweedfs.hcl)
nats_image=$(pin nats.hcl)
s3_port=${OVSQ_E2E_S3_PORT:-18335}
ov_port=${OVSQ_E2E_OV_PORT:-18080}
mon_port=${OVSQ_E2E_NATS_MON_PORT:-18222}

run_id=ovsq-pipe-$$
work=$(mktemp -d)
cleanup() {
    status=$?
    if [ $status -ne 0 ]; then
        for c in receiver consumer seaweed; do
            echo "--- $c log (last 30 lines)"
            docker logs --tail 30 "$run_id-$c" 2>&1 || true
        done
    fi
    docker rm -f "$run_id-nats" "$run_id-receiver" "$run_id-consumer" "$run_id-seaweed" >/dev/null 2>&1 || true
    docker network rm "$run_id" >/dev/null 2>&1 || true
    rm -rf "$work"
    exit $status
}
trap cleanup EXIT

step() { echo "== $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

step "building ovsq, the receiver and the consumer harness"
arch=$(docker version --format '{{.Server.Arch}}')
(cd "$here/.." && go build -o "$work/ovsq" .)
cp -R "$app" "$work/consumer-src"
mkdir -p "$work/consumer-src/cmd/ovsqe2e" "$work/bin"
cp "$here/pipeline/main.go" "$work/consumer-src/cmd/ovsqe2e/main.go"
(cd "$work/consumer-src" &&
    CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -o "$work/bin/ov-sync-consumer" . &&
    CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -o "$work/bin/harness" ./cmd/ovsqe2e/main.go)

echo hook-token > "$work/webhook_token"
echo ov-token > "$work/ov_token"
echo '{"JasperHG90/e2e/": "viking://user/jasper/resources/projects/e2e"}' > "$work/map.json"
sed "s#@TOKEN@#hook-token#; s#@RECEIVER@#$run_id-receiver#" "$here/notification.toml" > "$work/notification.toml"

step "starting NATS ($nats_image), the receiver and the consumer"
docker network create "$run_id" >/dev/null
docker run -d --name "$run_id-nats" --network "$run_id" -p "$mon_port:8222" \
    "$nats_image" -js -m 8222 >/dev/null
docker run -d --name "$run_id-receiver" --network "$run_id" \
    -v "$work/bin:/bin-e2e:ro" -v "$work/webhook_token:/secrets/webhook_token:ro" \
    -e NATS_URL="nats://$run_id-nats:4222" -e RECEIVER_ADDR=0.0.0.0:20500 \
    -e RECEIVER_TOKEN_FILE=/secrets/webhook_token \
    alpine:3 /bin-e2e/ov-sync-consumer receiver >/dev/null
docker run -d --name "$run_id-consumer" --network "$run_id" -p "$ov_port:8080" \
    -v "$work/bin:/bin-e2e:ro" -v "$work/ov_token:/secrets/ov_token:ro" -v "$work/map.json:/secrets/map.json:ro" \
    -e NATS_URL="nats://$run_id-nats:4222" -e FAKE_OV_ADDR=0.0.0.0:8080 \
    -e OV_TOKEN_FILE=/secrets/ov_token -e OV_SYNC_MAP_FILE=/secrets/map.json \
    -e S3_ENDPOINT="$run_id-seaweed:8333" -e S3_ACCESS_KEY=adminkey -e S3_SECRET_KEY=adminsecret \
    alpine:3 /bin-e2e/harness >/dev/null

# The consumer creates the stream; until it has, the receiver refuses events.
for _ in $(seq 60); do
    curl -sf "http://127.0.0.1:$mon_port/jsz?streams=true" | jq -e '.account_details[0].stream_detail[]? | select(.name == "OV_SYNC")' >/dev/null 2>&1 && break
    sleep 1
done
curl -sf "http://127.0.0.1:$mon_port/jsz?streams=true" | jq -e '.account_details[0].stream_detail[]? | select(.name == "OV_SYNC")' >/dev/null ||
    fail "the consumer never created the OV_SYNC stream"

step "starting SeaweedFS ($seaweed_image) with its filer webhook"
docker run -d --name "$run_id-seaweed" --network "$run_id" -p "$s3_port:8333" \
    -v "$here/s3.json:/etc/s3.json:ro" -v "$work/notification.toml:/etc/seaweedfs/notification.toml:ro" \
    "$seaweed_image" server -s3 -s3.config=/etc/s3.json >/dev/null
for _ in $(seq 60); do curl -s -o /dev/null "http://127.0.0.1:$s3_port/" && break; sleep 1; done
sleep 3
echo "s3.bucket.create -name ov-sync" | docker exec -i "$run_id-seaweed" weed shell >/dev/null

files() { curl -sf "http://127.0.0.1:$ov_port/_files"; }
calls() { curl -sf "http://127.0.0.1:$ov_port/_calls"; }
# wait_for JSON: until the fake OpenViking holds exactly these files.
wait_for() {
    for _ in $(seq 60); do
        jq -e --argjson want "$1" '. == $want' <<<"$(files)" >/dev/null && return 0
        sleep 1
    done
    echo "want: $1" >&2
    echo "got:  $(files)" >&2
    echo "calls: $(calls)" >&2
    fail "OpenViking never matched the folder"
}

ov=viking://user/jasper/resources/projects/e2e
mkdir -p "$work/folder/docs/deep" && cd "$work/folder"
printf '# hello\n' > docs/a.md
printf 'b\n' > b.md
printf 'c\n' > docs/deep/c.md
"$work/ovsq" init . >/dev/null
sed -i.bak -e 's#^prefix = ""#prefix = "JasperHG90/e2e"#' -e 's#^subdir = ""#subdir = "notes"#' \
    -e "s#^endpoint = .*#endpoint = \"http://127.0.0.1:$s3_port\"#" ov-sync-queue.toml
rm ov-sync-queue.toml.bak
grep -q '^subdir = "notes"' ov-sync-queue.toml || fail "could not set target.subdir in the starter config"
export AWS_ACCESS_KEY_ID=ovsqkey AWS_SECRET_ACCESS_KEY=ovsqsecret

step "ovsq run: three new files reach OpenViking under notes/"
"$work/ovsq" run . | tail -1
wait_for "$(jq -n --arg r "$ov/notes" '{($r+"/b.md"): "b\n", ($r+"/docs/a.md"): "# hello\n", ($r+"/docs/deep/c.md"): "c\n"}')"

step "ovsq run --delete: an edit and two deletes reach OpenViking"
printf 'changed\n' > docs/a.md
rm b.md docs/deep/c.md
"$work/ovsq" run . --delete | tail -1
wait_for "$(jq -n --arg r "$ov/notes" '{($r+"/docs/a.md"): "changed\n"}')"

step "checking what went through"
# Let any stragglers land before reading the logs and the stream.
sleep 3
all=$(calls)
jq -e 'map(select(test("_state"))) | length == 0' <<<"$all" >/dev/null ||
    fail "OpenViking got a call for ovsq's manifest: $all"
jq -e 'map(select(startswith("UNEXPECTED"))) | length == 0' <<<"$all" >/dev/null ||
    fail "OpenViking got a call the fake does not know: $all"
dirs=$(curl -sf "http://127.0.0.1:$ov_port/_dirs")
jq -e --arg r "$ov/notes/docs/deep" 'has($r) | not' <<<"$dirs" >/dev/null ||
    fail "the emptied folder docs/deep was left in OpenViking: $all"
jq -e --arg r "$ov/notes/docs" 'has($r)' <<<"$dirs" >/dev/null ||
    fail "the folder docs, which still holds a.md, was removed: $all"
if docker logs "$run_id-consumer" 2>&1 | grep -q 'dead-lettering'; then
    fail "the consumer dead-lettered an event"
fi
msgs=$(curl -sf "http://127.0.0.1:$mon_port/jsz?streams=true" |
    jq '.account_details[0].stream_detail[] | select(.name == "OV_SYNC") | .state.messages')
[ "$msgs" -ge 6 ] || fail "OV_SYNC holds $msgs events; want at least 6 (3 uploads, 1 edit, 2 deletes)"
out=$("$work/ovsq" status .)
grep -qF "0 to upload, 0 to delete, 1 unchanged" <<<"$out" || fail "ovsq status after the runs: $out"

echo "PASS: ovsq -> SeaweedFS -> receiver -> NATS ($msgs events on OV_SYNC) -> ov-sync-consumer -> OpenViking"
echo "      calls: $(jq -c . <<<"$all")"
