---
description: "Sync a local folder into OpenViking through the ov-sync queue with ovsq and 15-minute S3 credentials from your Vault session, then check the whole pipeline on your laptop."
stale_after: 2027-02-27
sources:
  - resource: packages/ov-sync-queue/README.md
  - resource: packages/ov-sync-queue/e2e/pipeline.sh
  - resource: packages/ov-sync-queue/internal/s3store/s3store.go
  - resource: packages/ov-sync-queue/main.go
  - resource: .github/workflows/release.yaml
---

# Set up ovsq

This guide gets one local folder into OpenViking through the ov-sync queue.
`ovsq` puts the files in the `ov-sync` bucket, and the pipeline in
JasperHG90/localstack takes them from there.

`ovsq` runs on your laptop, never in CI. In CI, use the `openviking-sync`
action.

No S3 key is stored for it. Before each run,
`localstack token s3 local-ov-sync` trades your Vault session for S3
credentials that last 15 minutes and can read, write and delete under `ov-sync/JasperHG90/local/` only. Localstack maps that
prefix once to `viking://user/jasper/resources/local/`, and each folder picks
its own place under it with `subdir`. So adding a folder needs no change in
localstack.

## Before you start

- The `localstack` CLI, logged in as the operator (`localstack login`).
- For the local check at the end: Docker, Go 1.26 or later, `jq` and
  `terraform` on PATH, and
  the localstack clone beside this repo (or `LOCALSTACK_DIR` pointing at it).

## Steps

### 1. Install ovsq

Download the archive for your platform from the newest
`ov-sync-queue-v*` release on
[the releases page](https://github.com/JasperHG90/openviking_extensions/releases).
Each holds one `ovsq` binary, for `darwin-arm64`, `darwin-amd64`,
`linux-amd64` or `linux-arm64`:

```bash
# version: the newest ov-sync-queue-v* tag on the releases page.
# platform: darwin-arm64, darwin-amd64, linux-amd64 or linux-arm64.
version=0.1.0 platform=darwin-arm64
mkdir -p ~/.local/bin
curl -fsSL "https://github.com/JasperHG90/openviking_extensions/releases/download/ov-sync-queue-v$version/ovsq-$version-$platform.tar.gz" |
    tar -xz -C ~/.local/bin ovsq
ovsq version                 # needs ~/.local/bin on your PATH
```

Or build it from source with Go 1.26 or later:

```bash
cd packages/ov-sync-queue
go build -o ~/.local/bin/ovsq .
```

### 2. Configure the folder

```console
$ ovsq init ~/notes
```

Edit `~/notes/ov-sync-queue.toml`:

```toml
[target]
prefix = "JasperHG90/local"
subdir = "notes"     # lands in viking://user/jasper/resources/local/notes/
source = "notes"     # names this folder's manifest
```

Give every folder its own `subdir` and `source`, or they overwrite and
delete each other's files. Every key is in
[the reference](../reference/ovsq-commands-and-config.md).

The prefix must stay `JasperHG90/local`: the credentials reach nothing else.
A place outside `resources/local/` is not possible from here. The consumer
deletes every file under a mapped folder that has no object behind it, so
localstack never maps a prefix over a folder that holds other content.

### 3. Load the credentials

In the shell that runs `ovsq`:

```console
$ eval "$(localstack token s3 local-ov-sync)"
local-ov-sync credentials expire at 2026-11-27T18:15:00Z
```

It exports `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and
`AWS_SESSION_TOKEN`, which `ovsq` reads first. A run that outlasts the 15
minutes fails with `ExpiredToken`: run the `eval` again and rerun `ovsq`,
which continues from its last manifest checkpoint.

### 4. Sync

```console
$ ovsq status ~/notes          # what would change; writes nothing
$ ovsq run ~/notes             # upload new and changed files
$ ovsq run ~/notes --delete    # also remove files you deleted locally
```

## Check it worked

- `ovsq status ~/notes` after a run shows `0 to upload, 0 to delete`.
- The files appear under `viking://user/jasper/resources/local/<subdir>/`,
  for example `viking://user/jasper/resources/local/notes/`. The consumer
  rate-limits its writes, so a large folder takes a while.
- An `Access Denied` from `ovsq` means the prefix in `ov-sync-queue.toml` is
  not `JasperHG90/local`. `ExpiredToken` means the credentials are past
  their 15 minutes: repeat step 3.

### Run the whole pipeline on your laptop

`e2e/pipeline.sh` runs `ovsq` through the whole pipeline in Docker. It is
local only and is not part of CI:

```console
$ cd packages/ov-sync-queue
$ ./e2e/pipeline.sh
```

It reads the set-up from your localstack clone:

- SeaweedFS's args, webhook config and IAM come from the jobspecs and
  Terraform.
- The receiver and the consumer run the `ov-sync-consumer` image that
  `services.tf` deploys.
- `ovsq` writes with a static key scoped to its prefix. That is a
  stand-in: the test has no Vault to mint the STS credentials step 3 uses.
  The test checks that the key can read, write and delete inside the
  prefix, and is refused outside it.

The deployed image is built for linux/arm64. On an amd64 machine Docker
needs emulation (Docker Desktop has it), or set `OVSQ_E2E_CONSUMER=source`.

Only OpenViking and the Nomad token issuer are faked.

It ends with `PASS:` and the images it ran. Useful settings:

| Variable | Effect |
|---|---|
| `LOCALSTACK_DIR` | The localstack clone. Default: `../localstack` beside this repo. |
| `OVSQ_E2E_CONSUMER=source` | Build the receiver and consumer from the clone's `applications/ov-sync-consumer` instead of pulling the deployed image, to test a change before it is released. |
| `OVSQ_E2E_KEEP=1` | Leave the containers up after the run, to look around. |
| `OVSQ_E2E_S3_PORT`, `OVSQ_E2E_OV_PORT`, `OVSQ_E2E_NATS_MON_PORT` | Host ports, if the defaults (18335, 18080, 18222) are taken. |
