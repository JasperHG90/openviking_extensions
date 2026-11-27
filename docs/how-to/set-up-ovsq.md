---
description: "Sync a local folder into OpenViking through the ov-sync queue with ovsq and an S3 key scoped to your prefix, then check the whole pipeline on your laptop."
stale_after: 2027-02-27
sources:
  - resource: packages/ov-sync-queue/README.md
  - resource: packages/ov-sync-queue/e2e/pipeline.sh
  - resource: packages/ov-sync-queue/internal/s3store/s3store.go
---

# Set up ovsq

This guide gets one local folder into OpenViking through the ov-sync queue.
`ovsq` puts the files in the `ov-sync` bucket, and the pipeline in
JasperHG90/localstack takes them from there.

`ovsq` runs on your laptop, never in CI. In CI, use the `openviking-sync`
action, which logs in without a key.

It authenticates with a static S3 key. No local route skips the key: the
keyless logins in localstack are for Nomad jobs (a workload token) and
GitHub Actions (a Vault token), and a laptop has neither. So you give it a
key scoped to one prefix, which can read, write and delete under that
prefix and nothing else.

The examples use the prefix `JasperHG90/notes`. Use `<owner>/<name>` for your
own.

## Before you start

- Go 1.26 or later, to build `ovsq`.
- A clone of JasperHG90/localstack you can apply Terraform from, and Vault
  access to read the key back.
- For the local check at the end: Docker, `jq` and `terraform` on PATH, and
  the localstack clone beside this repo (or `LOCALSTACK_DIR` pointing at it).

## Steps

### 1. Build ovsq

```bash
cd packages/ov-sync-queue
go build -o ~/.local/bin/ovsq .
```

### 2. Map your prefix to an OpenViking folder

The consumer drops files whose prefix it has no mapping for, and they reach
the bucket and go no further. In localstack, add the prefix to `prefixes` in
`nomad_job.ov_sync_consumer` (`deployments/applications/services.tf`):

```hcl
prefixes = jsonencode({
  # ...the entries already there
  "JasperHG90/notes/" = "viking://user/jasper/resources/projects/notes/"
})
```

Keep the trailing `/` on both sides.

### 3. Create a key scoped to your prefix

SeaweedFS reads its static keys from the `s3.json` template in
`deployments/infrastructure/services/seaweedfs.hcl`. Today that file holds
only the admin key. Do not hand the admin key to `ovsq`: it can do anything
to every bucket.

In `deployments/infrastructure/secrets.tf`, add a secret beside the admin
key's. Put it under `default/seaweedfs/` so the SeaweedFS job can read it,
because a Nomad job reads only `secret/data/default/<its job id>/*`:

```hcl
resource "random_password" "ovsq_notes_secret_key" {
  length  = 40
  special = false
}

resource "vault_kv_secret_v2" "ovsq_notes" {
  mount = vault_mount.kvv2.path
  name  = "default/seaweedfs/ovsq-notes"
  data_json = jsonencode({
    access_key = "ovsq-notes"
    secret_key = random_password.ovsq_notes_secret_key.result
  })
}
```

Pass its path to the SeaweedFS template. In
`deployments/infrastructure/services.tf`, add the variable next to
`seaweedfs_secret`:

```hcl
      ovsq_notes_secret = vault_kv_secret_v2.ovsq_notes.path
```

Then add an identity to `seaweedfs.hcl`'s `s3.json` template. The whole
file sits on one line, and invalid JSON there breaks S3 for every user once
SeaweedFS restarts, so edit it with care. Insert a comma and the new entry
just before the closing `]}`, so the line ends like this:

```json
...,"actions":["Admin","Read","Write","List","Tagging"]},{"name":"ovsq-notes","credentials":[{"accessKey":"{{ with secret "${ovsq_notes_secret}" }}{{ .Data.data.access_key }}{{ end }}","secretKey":"{{ with secret "${ovsq_notes_secret}" }}{{ .Data.data.secret_key }}{{ end }}"}],"actions":["Read:ov-sync/JasperHG90/notes/*","Write:ov-sync/JasperHG90/notes/*"]}]}
```

`Write` covers deletes. `ovsq` never lists the bucket, so the key needs no
`List`. Apply both Terraform roots. The SeaweedFS job restarts with the new
`s3.json`, and the consumer job picks up the new prefix map.

### 4. Configure the folder

```console
$ ovsq init ~/notes
```

Edit `~/notes/ov-sync-queue.toml` and set the prefix:

```toml
[target]
prefix = "JasperHG90/notes"
```

`subdir`, `source` and the `[sync]` filters are optional. Every key is in
[the reference](../reference/ovsq-commands-and-config.md).

### 5. Give ovsq the key

Read the key back from Vault:

```console
$ vault kv get -mount=secret default/seaweedfs/ovsq-notes
```

Then export it in the shell that runs `ovsq`:

```bash
export AWS_ACCESS_KEY_ID=ovsq-notes
export AWS_SECRET_ACCESS_KEY=<secret_key from Vault>
```

Or put it in a profile in `~/.aws/credentials` and set `AWS_PROFILE`.
Exported variables win over the profile, so unset `AWS_ACCESS_KEY_ID` and
`MINIO_ROOT_USER` in that shell if you use one. Never
put the key in `ov-sync-queue.toml`: it has no field for one.

### 6. Sync

```console
$ ovsq status ~/notes          # what would change; writes nothing
$ ovsq run ~/notes             # upload new and changed files
$ ovsq run ~/notes --delete    # also remove files you deleted locally
```

## Check it worked

- `ovsq status ~/notes` after a run shows `0 to upload, 0 to delete`.
- The files appear under the folder you mapped in step 2, for example
  `viking://user/jasper/resources/projects/notes/`. The consumer
  rate-limits its writes, so a large folder takes a while.
- An `Access Denied` from `ovsq` means the key's actions do not cover the
  prefix in `ov-sync-queue.toml`. Check the two match exactly.

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
- `ovsq` writes with a key scoped to its prefix, shaped like the one in
  step 3. The test checks that the key can read, write and delete inside
  the prefix, and is refused outside it.

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
