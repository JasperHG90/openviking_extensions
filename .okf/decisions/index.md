# Decisions
- [ADR 0001: Keep project knowledge in an OKF bundle](0001-keep-project-knowledge-in-an-okf-bundle.md): Project knowledge lives in an OKF bundle in .okf/, next to the code, not in a wiki or README notes.
- [ADR 0002: ov-sync-queue syncs into the ov-sync bucket, not straight to the queue](0002-ov-sync-queue-syncs-into-the-ov-sync-bucket-not-straight-to-the-queue.md): ovsq uploads files to the ov-sync S3 bucket, whose webhook queues them for ov-sync-consumer, instead of publishing to JetStream or writing to OpenViking.
- [ADR 0003: Each ovsq source owns its own manifest in the bucket](0003-each-ovsq-source-owns-its-own-manifest-in-the-bucket.md): ovsq keeps the CI action manifest format but writes it to <prefix>/_state/ovsq-<source>.json, with a local cache only to skip re-hashing.
- [ADR 0004: ovsq is written in Go](0004-ovsq-is-written-in-go.md): The bucket sync is a Go module in packages/, unlike the Python ov-sync, matching ov-sync-consumer and its minio-go client.
