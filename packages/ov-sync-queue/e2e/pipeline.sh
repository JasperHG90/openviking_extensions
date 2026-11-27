#!/usr/bin/env bash
# LOCAL ONLY: not in CI and not a gate. It needs docker, Go, jq and a clone
# of JasperHG90/localstack (LOCALSTACK_DIR, default ../localstack beside this
# repo), because it builds ov-sync-consumer from that clone and reads the
# production SeaweedFS set-up from its jobspecs.
#
# Runs ovsq through the whole ov-sync pipeline, each piece in a container:
#
#   ovsq -> SeaweedFS -(filer webhook, loopback)-> receiver -> NATS JetStream
#        -> ov-sync-consumer -> fake OpenViking
#
# What matches production:
# - SeaweedFS and NATS run the images localstack pins, and SeaweedFS runs the
#   jobspec's own args, webhook config and master.toml, read from
#   seaweedfs.hcl. As in production, SeaweedFS shares the receiver's network
#   namespace and posts its webhook to the receiver on 127.0.0.1.
# - The receiver and consumer are the real ov-sync-consumer binary, run with
#   the jobspec's environment. The consumer gets its S3 credentials the
#   production way: SeaweedFS's STS trades a Nomad-style workload token for
#   the ov-sync-consumer role, under the IAM policy services.tf renders
#   (defaultEffect Deny). The script refuses to run if services.tf's role,
#   policy or trust lines change, so its copy cannot drift silently.
# What is faked: OpenViking, and the Nomad OIDC issuer that signs the token
# (pipeline/main.go). The catch-up pass is pushed out to 24h, so every change
# that reaches OpenViking got there through an event.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
localstack=${LOCALSTACK_DIR:-$(dirname "$repo")/localstack}
app=$localstack/applications/ov-sync-consumer
infra=$localstack/deployments/infrastructure
seaweed_hcl=$infra/services/seaweedfs.hcl
consumer_hcl=$localstack/deployments/applications/services/ov-sync-consumer.hcl
for f in "$app/go.mod" "$seaweed_hcl" "$infra/services.tf" "$consumer_hcl"; do
    [ -f "$f" ] || { echo "missing $f: set LOCALSTACK_DIR to a localstack clone" >&2; exit 2; }
done

step() { echo "== $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# The IAM copy below must match services.tf's local.seaweedfs_iam.
for line in \
    'policy = { defaultEffect = "Deny" }' \
    'config  = { issuer = "https://nomad.lab.orangecluster.nl", clientId = "seaweedfs" }' \
    '"ov-sync-consumer" = "ov-sync"' \
    'Action   = ["s3:*"]' \
    'Resource = ["arn:aws:s3:::${bucket}", "arn:aws:s3:::${bucket}/*"]' \
    'roleArn  = "arn:aws:iam::role/${job}"' \
    'Principal = { Federated = "nomad" }' \
    'StringEquals = { "oidc:nomad_job_id" = job }' \
    'StringLike   = { "oidc:sub" = "global:default:${job}:*" }'; do
    grep -qF -- "$line" "$infra/services.tf" ||
        fail "services.tf no longer has: $line. Update the IAM copy in pipeline.sh to match."
done
for line in 'AWS_ROLE_ARN                = "arn:aws:iam::role/ov-sync-consumer"' 'aud         = ["seaweedfs"]'; do
    grep -qF -- "$line" "$consumer_hcl" ||
        fail "ov-sync-consumer.hcl no longer has: $line. Update pipeline.sh to match."
done

pin() { grep -Eo 'docker\.io/[a-z0-9/._-]+:[A-Za-z0-9._-]+' "$1" | head -1; }
seaweed_image=$(pin "$seaweed_hcl")
nats_image=$(pin "$infra/services/nats.hcl")
s3_port=${OVSQ_E2E_S3_PORT:-18335}
ov_port=${OVSQ_E2E_OV_PORT:-18080}
mon_port=${OVSQ_E2E_NATS_MON_PORT:-18222}

run_id=ovsq-pipe-$$
work=$(mktemp -d)
cleanup() {
    status=$?
    if [ $status -ne 0 ]; then
        docker ps -a --filter "name=$run_id" --format "{{.Names}}: {{.Status}}"
        for c in receiver consumer seaweed fakes; do
            echo "--- $c log (last 30 lines)"
            docker logs --tail 30 "$run_id-$c" 2>&1 || true
        done
    fi
    if [ -n "${OVSQ_E2E_KEEP:-}" ]; then
        echo "OVSQ_E2E_KEEP is set: leaving the $run_id-* containers and $work"
        exit $status
    fi
    for c in seaweed consumer receiver nats fakes; do docker rm -f "$run_id-$c" >/dev/null 2>&1 || true; done
    docker network rm "$run_id" >/dev/null 2>&1 || true
    rm -rf "$work"
    exit $status
}
trap cleanup EXIT

step "building ovsq, ov-sync-consumer and the fakes"
arch=$(docker version --format '{{.Server.Arch}}')
mkdir -p "$work/bin" "$work/secrets" "$work/tokens"
(cd "$here/.." && go build -o "$work/ovsq" . &&
    CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -o "$work/bin/fakes" ./e2e/pipeline)
(cd "$app" && CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -o "$work/bin/ov-sync-consumer" .)

step "rendering SeaweedFS's config from seaweedfs.hcl and services.tf"
seaweed_args=() # a loop, not mapfile: macOS ships bash 3.2
while IFS= read -r arg; do seaweed_args+=("$arg"); done < <(
    awk '/config \{/{c=1} c&&/args = \[/{a=1;next} a&&/\]/{exit} a' "$seaweed_hcl" | grep -o '"[^"]*"' | tr -d '"')
[ "${seaweed_args[0]:-}" = server ] || fail "could not read SeaweedFS's args from seaweedfs.hcl"
awk '/\[notification.webhook\]/{p=1} p{print} /path_prefixes/{exit}' "$seaweed_hcl" | sed 's/^ *//' |
    sed 's/^bearer_token = .*/bearer_token = "hook-token"/' > "$work/secrets/notification.toml"
grep -qF 'endpoint = "http://127.0.0.1:20500/events"' "$work/secrets/notification.toml" ||
    fail "the webhook no longer posts to the receiver on loopback: $(cat "$work/secrets/notification.toml")"
awk '/\[master.volume_growth\]/{p=1} p&&/EOH/{exit} p' "$seaweed_hcl" | sed 's/^ *//' > "$work/secrets/master.toml"
issuer=http://$run_id-fakes:8090
jq -n --arg issuer "$issuer" '{
  sts: {tokenDuration: "1h", maxSessionLength: "12h", issuer: "seaweedfs-sts",
        signingKey: "c2VhdHdlZWRmcy1lMmUtc2lnbmluZy1rZXktMzJieXRlcyE="},
  policy: {defaultEffect: "Deny"},
  providers: [{name: "nomad", type: "oidc", enabled: true, config: {issuer: $issuer, clientId: "seaweedfs"}}],
  policies: [{name: "ov-sync-consumer", document: {Version: "2012-10-17", Statement: [{
    Effect: "Allow", Action: ["s3:*"], Resource: ["arn:aws:s3:::ov-sync", "arn:aws:s3:::ov-sync/*"]}]}}],
  roles: [{roleName: "ov-sync-consumer", roleArn: "arn:aws:iam::role/ov-sync-consumer",
    trustPolicy: {Version: "2012-10-17", Statement: [{
      Effect: "Allow", Principal: {Federated: "nomad"}, Action: "sts:AssumeRoleWithWebIdentity",
      Condition: {StringEquals: {"oidc:nomad_job_id": "ov-sync-consumer"},
                  StringLike: {"oidc:sub": "global:default:ov-sync-consumer:*"}}}]},
    attachedPolicies: ["ov-sync-consumer"]}]
}' > "$work/secrets/iam.json"
cp "$here/s3.json" "$work/secrets/s3.json"
echo hook-token > "$work/secrets/webhook_token"
echo ov-token > "$work/secrets/ov_token"
echo '{"JasperHG90/e2e/": "viking://user/jasper/resources/projects/e2e"}' > "$work/secrets/prefixes.json"

step "starting the fakes, NATS ($nats_image), the receiver and the consumer"
docker network create "$run_id" >/dev/null
docker run -d --name "$run_id-fakes" --network "$run_id" -p "$ov_port:8080" \
    -v "$work/bin:/bin-e2e:ro" -v "$work/tokens:/tokens" \
    -e FAKE_OV_ADDR=0.0.0.0:8080 -e ISSUER_ADDR=0.0.0.0:8090 -e ISSUER="$issuer" \
    -e TOKEN_FILE=/tokens/nomad_seaweedfs.jwt \
    alpine:3 /bin-e2e/fakes >/dev/null
docker run -d --name "$run_id-nats" --network "$run_id" -p "$mon_port:8222" \
    "$nats_image" -js -m 8222 >/dev/null
# The receiver owns the network namespace SeaweedFS joins, as in the
# jobspec, so S3's port is published here.
docker run -d --name "$run_id-receiver" --network "$run_id" -p "$s3_port:8333" \
    -v "$work/bin:/bin-e2e:ro" -v "$work/secrets/webhook_token:/secrets/webhook_token:ro" \
    -e NATS_URL="nats://$run_id-nats:4222" -e RECEIVER_TOKEN_FILE=/secrets/webhook_token \
    alpine:3 /bin-e2e/ov-sync-consumer receiver >/dev/null
for _ in $(seq 30); do [ -s "$work/tokens/nomad_seaweedfs.jwt" ] && break; sleep 1; done
docker run -d --name "$run_id-consumer" --network "$run_id" \
    -v "$work/bin:/bin-e2e:ro" -v "$work/tokens:/secrets/tokens:ro" \
    -v "$work/secrets/ov_token:/secrets/ov_token:ro" -v "$work/secrets/prefixes.json:/local/prefixes.json:ro" \
    -e NATS_URL="nats://$run_id-nats:4222" \
    -e OV_URL="http://$run_id-fakes:8080" -e OV_TOKEN_FILE=/secrets/ov_token \
    -e OV_SYNC_MAP_FILE=/local/prefixes.json \
    -e S3_ENDPOINT="$run_id-receiver:8333" -e S3_BUCKET=ov-sync \
    -e AWS_WEB_IDENTITY_TOKEN_FILE=/secrets/tokens/nomad_seaweedfs.jwt \
    -e AWS_ROLE_ARN=arn:aws:iam::role/ov-sync-consumer \
    -e RATE_PER_SECOND=50 -e MAX_TRIES=3 -e CATCHUP_INTERVAL=24h \
    alpine:3 /bin-e2e/ov-sync-consumer consumer >/dev/null

stream_up() {
    curl -sf "http://127.0.0.1:$mon_port/jsz?streams=true" |
        jq -e '.account_details[0].stream_detail[]? | select(.name == "OV_SYNC")' >/dev/null 2>&1
}
for _ in $(seq 60); do stream_up && break; sleep 1; done
stream_up || fail "the consumer never created the OV_SYNC stream"

step "starting SeaweedFS ($seaweed_image) in the receiver's namespace, with the jobspec's args"
docker run -d --name "$run_id-seaweed" --network "container:$run_id-receiver" \
    -v "$work/secrets/s3.json:/secrets/s3.json:ro" -v "$work/secrets/iam.json:/secrets/iam.json:ro" \
    -v "$work/secrets/master.toml:/etc/seaweedfs/master.toml:ro" \
    -v "$work/secrets/notification.toml:/etc/seaweedfs/notification.toml:ro" \
    "$seaweed_image" "${seaweed_args[@]}" >/dev/null
for _ in $(seq 60); do curl -s -o /dev/null "http://127.0.0.1:$s3_port/" && break; sleep 1; done
created=
for _ in $(seq 30); do
    # Capture before grepping: grep -q under pipefail can SIGPIPE weed shell.
    echo "s3.bucket.create -name ov-sync" | docker exec -i "$run_id-seaweed" weed shell >/dev/null 2>&1 || true
    buckets=$(echo "s3.bucket.list" | docker exec -i "$run_id-seaweed" weed shell 2>/dev/null || true)
    if grep -q 'ov-sync' <<<"$buckets"; then created=1; break; fi
    sleep 1
done
[ -n "$created" ] || fail "could not create the ov-sync bucket"

files() { curl -sf "http://127.0.0.1:$ov_port/_files"; }
dirs() { curl -sf "http://127.0.0.1:$ov_port/_dirs"; }
calls() { curl -sf "http://127.0.0.1:$ov_port/_calls"; }
# wait_for JSON: until the fake OpenViking holds exactly these files.
wait_for() {
    for _ in $(seq 90); do
        jq -e --argjson want "$1" '. == $want' <<<"$(files)" >/dev/null && return 0
        sleep 1
    done
    echo "want:  $1" >&2
    echo "got:   $(files)" >&2
    echo "calls: $(calls)" >&2
    fail "OpenViking never matched the folder"
}
sha() { echo "sha256:$(shasum -a 256 "$1" | cut -d' ' -f1)"; }
png() { printf '\x89PNG\r\n\x1a\n' > "$1"; head -c 4096 /dev/urandom >> "$1"; }

r=viking://user/jasper/resources/projects/e2e/notes
mkdir -p "$work/folder/docs/deep" "$work/folder/img" && cd "$work/folder"
printf '# hello\n' > docs/a.md
printf 'b\n' > b.md
printf 'c\n' > docs/deep/c.md
png img/pic.png
"$work/ovsq" init . >/dev/null
sed -i.bak -e 's#^prefix = ""#prefix = "JasperHG90/e2e"#' -e 's#^subdir = ""#subdir = "notes"#' \
    -e "s#^endpoint = .*#endpoint = \"http://127.0.0.1:$s3_port\"#" ov-sync-queue.toml
rm ov-sync-queue.toml.bak
grep -q '^subdir = "notes"' ov-sync-queue.toml || fail "could not set target.subdir in the starter config"
export AWS_ACCESS_KEY_ID=ovsqkey AWS_SECRET_ACCESS_KEY=ovsqsecret

step "ovsq run: three text files and an image reach OpenViking under notes/"
"$work/ovsq" run . | tail -1
wait_for "$(jq -n --arg r "$r" --arg pic "$(sha img/pic.png)" \
    '{($r+"/b.md"): "b\n", ($r+"/docs/a.md"): "# hello\n", ($r+"/docs/deep/c.md"): "c\n", ($r+"/img/pic.png/pic.png"): $pic}')"

step "ovsq run --delete: a text edit, a new image and two deletes reach OpenViking"
printf 'changed\n' > docs/a.md
png img/pic.png
rm b.md docs/deep/c.md
"$work/ovsq" run . --delete | tail -1
wait_for "$(jq -n --arg r "$r" --arg pic "$(sha img/pic.png)" \
    '{($r+"/docs/a.md"): "changed\n", ($r+"/img/pic.png/pic.png"): $pic}')"

step "ovsq run --delete: the image's delete reaches OpenViking"
rm img/pic.png
"$work/ovsq" run . --delete | tail -1
wait_for "$(jq -n --arg r "$r" '{($r+"/docs/a.md"): "changed\n"}')"

step "checking what went through"
sleep 3 # let any stragglers land before reading the logs and the stream
all=$(calls)
d=$(dirs)
jq -e 'map(select(test("_state"))) | length == 0' <<<"$all" >/dev/null ||
    fail "OpenViking got a call for ovsq's manifest: $all"
jq -e 'map(select(startswith("UNEXPECTED"))) | length == 0' <<<"$all" >/dev/null ||
    fail "OpenViking got a call the fake does not know: $all"
for gone in docs/deep img/pic.png img; do
    jq -e --arg u "$r/$gone" 'has($u) | not' <<<"$d" >/dev/null ||
        fail "the emptied folder $gone was left in OpenViking: $all"
done
jq -e --arg u "$r/docs" 'has($u)' <<<"$d" >/dev/null ||
    fail "the folder docs, which still holds a.md, was removed: $all"
# Capture first: under pipefail, grep -q exiting early can kill docker logs
# with SIGPIPE and hide the match.
consumer_log=$(docker logs "$run_id-consumer" 2>&1)
if grep -q 'dead-lettering' <<<"$consumer_log"; then
    fail "the consumer dead-lettered an event"
fi
msgs=$(curl -sf "http://127.0.0.1:$mon_port/jsz?streams=true" |
    jq '.account_details[0].stream_detail[] | select(.name == "OV_SYNC") | .state.messages')
# Folder and manifest events count too, so this only shows events flowed;
# wait_for above is what checks each change.
[ "$msgs" -ge 9 ] || fail "OV_SYNC holds $msgs events; nine file changes should give at least 9"
out=$("$work/ovsq" status .)
grep -qF "0 to upload, 0 to delete, 1 unchanged" <<<"$out" || fail "ovsq status after the runs: $out"

echo "PASS: ovsq -> SeaweedFS -> receiver (loopback) -> NATS ($msgs events on OV_SYNC) -> ov-sync-consumer (STS role) -> OpenViking"
echo "      calls: $(jq -c . <<<"$all")"
