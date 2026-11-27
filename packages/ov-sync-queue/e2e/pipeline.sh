#!/usr/bin/env bash
# LOCAL ONLY: not in CI and not a gate. It needs docker, Go, jq, terraform
# and a clone of JasperHG90/localstack (LOCALSTACK_DIR, default ../localstack
# beside this repo), because it reads the production set-up from that clone's
# jobspecs and Terraform.
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
# - The receiver and consumer run the ov-sync-consumer image services.tf
#   deploys, with the jobspec's environment. OVSQ_E2E_CONSUMER=source builds
#   them from the clone's applications/ov-sync-consumer instead, to test a
#   change before it is released.
# - ovsq writes with a static S3 key scoped to its prefix, as the how-to
#   docs/how-to/set-up-ovsq.md sets one up, and the test checks the key is
#   refused outside that prefix. The consumer gets its S3 credentials the
#   production way: SeaweedFS's STS trades a Nomad-style workload token for
#   its role. SeaweedFS's iam.json is built the way production builds it:
#   terraform evaluates services.tf's own iam_members expression over its
#   local.seaweedfs_iam, and the result fills seaweedfs.hcl's iam.json
#   template. Only the nomad provider's issuer is swapped for the fake one.
#   The role ARN, the token's audience and the job id come from
#   ov-sync-consumer.hcl.
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
for f in "$seaweed_hcl" "$infra/services.tf" "$consumer_hcl"; do
    [ -f "$f" ] || { echo "missing $f: set LOCALSTACK_DIR to a localstack clone" >&2; exit 2; }
done

step() { echo "== $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

hcl_value() { # hcl_value FILE KEY: the quoted value of the first KEY = "..." line
    grep -E "^[[:space:]]*$2[[:space:]]*=" "$1" | head -1 | sed -E 's/^[^"]*"([^"]*)".*/\1/'
}
role_arn=$(hcl_value "$consumer_hcl" AWS_ROLE_ARN)
job=$(grep -E '^job "' "$consumer_hcl" | sed -E 's/^job "([^"]+)".*/\1/')
aud=$(grep -E '^[[:space:]]*aud[[:space:]]*=' "$consumer_hcl" | head -1 | sed -E 's/.*\["([^"]+)"\].*/\1/')
[ -n "$role_arn" ] && [ -n "$job" ] && [ -n "$aud" ] ||
    fail "could not read the role ARN, job or audience from $consumer_hcl"

pin() { grep -Eo 'docker\.io/[a-z0-9/._-]+:[A-Za-z0-9._-]+' "$1" | head -1; }
seaweed_image=$(pin "$seaweed_hcl")
# deployed_image TF: the ov-sync-consumer image a services.tf deploys.
deployed_image() { hcl_value "$1" ov_sync_consumer_image; }
receiver_image=$(deployed_image "$infra/services.tf")
consumer_image=$(deployed_image "$localstack/deployments/applications/services.tf")
case ${OVSQ_E2E_CONSUMER:-deployed} in
deployed)
    [ -n "$receiver_image" ] && [ -n "$consumer_image" ] ||
        fail "could not read ov_sync_consumer_image from the services.tf files"
    ;;
source)
    [ -f "$app/go.mod" ] || fail "missing $app/go.mod: OVSQ_E2E_CONSUMER=source builds from it"
    receiver_image=alpine:3 consumer_image=alpine:3
    ;;
*) fail "OVSQ_E2E_CONSUMER must be deployed or source, not ${OVSQ_E2E_CONSUMER}" ;;
esac
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

step "building ovsq and the fakes"
arch=$(docker version --format '{{.Server.Arch}}')
mkdir -p "$work/bin" "$work/secrets" "$work/tokens"
(cd "$here/.." && go build -o "$work/ovsq" . &&
    CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -o "$work/bin/fakes" ./e2e/pipeline)
if [ "${OVSQ_E2E_CONSUMER:-deployed}" = source ]; then
    step "building ov-sync-consumer from $app"
    (cd "$app" && CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -o "$work/bin/ov-sync-consumer" .)
    consumer_cmd=/bin-e2e/ov-sync-consumer
else
    step "pulling $receiver_image and $consumer_image"
    docker pull -q "$receiver_image" >/dev/null && docker pull -q "$consumer_image" >/dev/null ||
        fail "could not pull the deployed ov-sync-consumer images"
    consumer_cmd= # the image's entrypoint is the binary
fi

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
# Production: services.tf passes iam_members (an expression over
# local.seaweedfs_iam) into seaweedfs.hcl's iam.json template. Evaluate that
# same expression with terraform over the locals block that defines it, with
# no CI callers, then fill the same template.
mkdir -p "$work/tf"
awk '/^locals \{/{buf=$0"\n"; inb=1; next}
     inb{buf=buf $0 "\n"; if ($0 ~ /^\}/) { if (buf ~ /seaweedfs_iam/) printf "%s", buf; inb=0; buf="" }}' \
    "$infra/services.tf" > "$work/tf/locals.tf"
printf 'variable "vault_ci_callers" {\n  type    = any\n  default = {}\n}\n' > "$work/tf/variables.tf"
members_expr=$(grep -E '^[[:space:]]*iam_members[[:space:]]*=' "$infra/services.tf" | sed -E 's/^[[:space:]]*iam_members[[:space:]]*=[[:space:]]*//')
[ -n "$members_expr" ] || fail "services.tf no longer passes iam_members to seaweedfs.hcl"
members=$(cd "$work/tf" && echo "jsonencode($members_expr)" | terraform console -no-color | jq -r . | jq -r .) ||
    fail "terraform could not evaluate iam_members over services.tf's locals"
template=$(grep -F '"sts":' "$seaweed_hcl" | sed -E 's/^[[:space:]]+//')
[ -n "$template" ] || fail "seaweedfs.hcl no longer has an iam.json template with an sts block"
template=${template#'{{ with secret "${sts_secret}" }}'}
template=${template%'{{ end }}'}
template=${template//'{{ .Data.data.signing_key }}'/c2VhdHdlZWRmcy1lMmUtc2lnbmluZy1rZXktMzJieXRlcyE=}
template=${template//'${iam_members}'/$members}
jq --arg issuer "$issuer" '.providers |= map(if .name == "nomad" then .config.issuer = $issuer else . end)' \
    <<<"$template" > "$work/secrets/iam.json" || fail "the rendered iam.json is not JSON: $template"
jq -e --arg issuer "$issuer" --arg role "$role_arn" '
    any(.providers[]; .name == "nomad" and .config.issuer == $issuer) and any(.roles[]; .roleArn == $role)' \
    "$work/secrets/iam.json" >/dev/null ||
    fail "the rendered iam.json has no nomad provider or no role $role_arn"
# Production's s3.json holds only the admin key; ovsq's identity is the one
# docs/how-to/set-up-ovsq.md adds, scoped to its prefix.
cat > "$work/secrets/s3.json" <<'EOF'
{"identities": [
  {"name": "admin", "credentials": [{"accessKey": "adminkey", "secretKey": "adminsecret"}],
   "actions": ["Admin", "Read", "Write", "List", "Tagging"]},
  {"name": "ovsq-e2e", "credentials": [{"accessKey": "ovsqkey", "secretKey": "ovsqsecret"}],
   "actions": ["Read:ov-sync/JasperHG90/e2e/*", "Write:ov-sync/JasperHG90/e2e/*"]}
]}
EOF
echo hook-token > "$work/secrets/webhook_token"
echo ov-token > "$work/secrets/ov_token"
echo '{"JasperHG90/e2e/": "viking://user/jasper/resources/projects/e2e"}' > "$work/secrets/prefixes.json"

step "starting the fakes, NATS ($nats_image), the receiver and the consumer"
docker network create "$run_id" >/dev/null
docker run -d --name "$run_id-fakes" --network "$run_id" -p "$ov_port:8080" \
    -v "$work/bin:/bin-e2e:ro" -v "$work/tokens:/tokens" \
    -e FAKE_OV_ADDR=0.0.0.0:8080 -e ISSUER_ADDR=0.0.0.0:8090 -e ISSUER="$issuer" \
    -e TOKEN_FILE=/tokens/nomad_seaweedfs.jwt -e JOB="$job" -e AUD="$aud" \
    alpine:3 /bin-e2e/fakes >/dev/null
docker run -d --name "$run_id-nats" --network "$run_id" -p "$mon_port:8222" \
    "$nats_image" -js -m 8222 >/dev/null
# The receiver owns the network namespace SeaweedFS joins, as in the
# jobspec, so S3's port is published here.
docker run -d --name "$run_id-receiver" --network "$run_id" -p "$s3_port:8333" \
    -v "$work/bin:/bin-e2e:ro" -v "$work/secrets/webhook_token:/secrets/webhook_token:ro" \
    -e NATS_URL="nats://$run_id-nats:4222" -e RECEIVER_TOKEN_FILE=/secrets/webhook_token \
    "$receiver_image" $consumer_cmd receiver >/dev/null
for _ in $(seq 30); do [ -s "$work/tokens/nomad_seaweedfs.jwt" ] && break; sleep 1; done
docker run -d --name "$run_id-consumer" --network "$run_id" \
    -v "$work/bin:/bin-e2e:ro" -v "$work/tokens:/secrets/tokens:ro" \
    -v "$work/secrets/ov_token:/secrets/ov_token:ro" -v "$work/secrets/prefixes.json:/local/prefixes.json:ro" \
    -e NATS_URL="nats://$run_id-nats:4222" \
    -e OV_URL="http://$run_id-fakes:8080" -e OV_TOKEN_FILE=/secrets/ov_token \
    -e OV_SYNC_MAP_FILE=/local/prefixes.json \
    -e S3_ENDPOINT="$run_id-receiver:8333" -e S3_BUCKET=ov-sync \
    -e AWS_WEB_IDENTITY_TOKEN_FILE=/secrets/tokens/nomad_seaweedfs.jwt \
    -e AWS_ROLE_ARN="$role_arn" \
    -e RATE_PER_SECOND=50 -e MAX_TRIES=3 -e CATCHUP_INTERVAL=24h \
    "$consumer_image" $consumer_cmd consumer >/dev/null

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

step "ovsq's key is refused outside its prefix"
mkdir -p "$work/other" && printf 'x\n' > "$work/other/x.md"
"$work/ovsq" init "$work/other" >/dev/null
sed -i.bak -e 's#^prefix = ""#prefix = "JasperHG90/other"#' \
    -e "s#^endpoint = .*#endpoint = \"http://127.0.0.1:$s3_port\"#" "$work/other/ov-sync-queue.toml"
grep -q '^prefix = "JasperHG90/other"' "$work/other/ov-sync-queue.toml" || fail "could not set target.prefix in the starter config"
denied=$("$work/ovsq" run "$work/other" 2>&1) && fail "ovsq wrote outside its prefix: $denied"
grep -qF 'Access Denied' <<<"$denied" || fail "ovsq failed outside its prefix, but not with Access Denied: $denied"
# ovsq stops at its manifest read, so try a write and a delete directly.
for method in PUT DELETE; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --aws-sigv4 "aws:amz:us-east-1:s3" --user ovsqkey:ovsqsecret \
        -X "$method" --data-binary x "http://127.0.0.1:$s3_port/ov-sync/JasperHG90/other/x.md")
    [ "$code" = 403 ] || fail "ovsq's key got HTTP $code for a $method outside its prefix, want 403"
done
code=$(curl -s -o /dev/null -w '%{http_code}' --aws-sigv4 "aws:amz:us-east-1:s3" --user ovsqkey:ovsqsecret \
    -X PUT --data-binary x "http://127.0.0.1:$s3_port/ov-sync/JasperHG90/e2e/_probe.md")
[ "$code" = 200 ] || fail "ovsq's key got HTTP $code for a PUT inside its prefix, want 200: the curl check is broken"
curl -s -o /dev/null --aws-sigv4 "aws:amz:us-east-1:s3" --user ovsqkey:ovsqsecret \
    -X DELETE "http://127.0.0.1:$s3_port/ov-sync/JasperHG90/e2e/_probe.md"

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
# Wait until the consumer has acked everything, so a retried or
# dead-lettered event has run its course before the logs are read.
drained() {
    curl -sf "http://127.0.0.1:$mon_port/jsz?consumers=true" | jq -e '
        [.account_details[0].stream_detail[] | select(.name == "OV_SYNC") | .consumer_detail[]]
        | length > 0 and all(.[]; .num_pending == 0 and .num_ack_pending == 0)' >/dev/null 2>&1
}
for _ in $(seq 120); do drained && break; sleep 1; done
drained || fail "the consumer never acked every event"
all=$(calls)
d=$(dirs)
jq -e 'map(select(test("_state"))) | length == 0' <<<"$all" >/dev/null ||
    fail "OpenViking got a call for ovsq's manifest: $all"
jq -e 'map(select(startswith("UNEXPECTED"))) | length == 0' <<<"$all" >/dev/null ||
    fail "OpenViking got a call the fake does not know: $all"
jq -e --arg u "$r/docs/deep" 'has($u) | not' <<<"$d" >/dev/null ||
    fail "the emptied folder docs/deep was left in OpenViking: $all"
jq -e --arg u "$r/docs" '.[$u] == "dir"' <<<"$d" >/dev/null ||
    fail "the folder docs, which still holds a.md, was removed: $all"
# OpenViking keeps a deleted image's folder as an empty ghost, so the consumer
# deletes img/pic.png, treats the ghost as gone, and leaves img alone.
jq -e --arg u "$r/img/pic.png" 'index("DELETE " + $u) != null' <<<"$all" >/dev/null ||
    fail "the consumer never deleted the image's folder img/pic.png: $all"
jq -e --arg u "$r/img/pic.png" '.[$u] == "ghost"' <<<"$d" >/dev/null ||
    fail "img/pic.png should be left as OpenViking's empty ghost folder: $d"
jq -e --arg u "$r/img" 'index("DELETE " + $u) == null' <<<"$all" >/dev/null ||
    fail "the consumer deleted img, which still lists the ghost: $all"
# Capture first: under pipefail, grep -q exiting early can kill docker logs
# with SIGPIPE and hide the match.
consumer_log=$(docker logs "$run_id-consumer" 2>&1)
if grep -qE 'event failed|dead-lettering' <<<"$consumer_log"; then
    grep -E 'event failed|dead-lettering' <<<"$consumer_log" | head -5 >&2
    fail "the consumer failed or dead-lettered an event"
fi
msgs=$(curl -sf "http://127.0.0.1:$mon_port/jsz?streams=true" |
    jq '.account_details[0].stream_detail[] | select(.name == "OV_SYNC") | .state.messages')
# Folder and manifest events count too, so this only shows events flowed;
# wait_for above is what checks each change.
[ "$msgs" -ge 9 ] || fail "OV_SYNC holds $msgs events; nine file changes should give at least 9"
out=$("$work/ovsq" status .)
grep -qF "0 to upload, 0 to delete, 1 unchanged" <<<"$out" || fail "ovsq status after the runs: $out"

echo "PASS: ovsq (scoped key) -> SeaweedFS -> receiver (loopback) -> NATS ($msgs events on OV_SYNC) -> ov-sync-consumer (STS role) -> OpenViking"
echo "      receiver: $receiver_image, consumer: $consumer_image${consumer_cmd:+ running the binary built from $app}"
echo "      calls: $(jq -c . <<<"$all")"
