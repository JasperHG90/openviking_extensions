#!/bin/sh
# Start SeaweedFS S3, run the minio-go checks against it, remove the container.
set -u
D=/private/tmp/claude-502/-Users-jasperginn-Documents-code-projects-openviking-extensions/38afc982-80df-4d50-bb9c-7efed4cd88b4/scratchpad/spike-r1
NAME=spike-r1-seaweed
docker rm -f $NAME >/dev/null 2>&1
docker run -d --name $NAME -p 18333:8333 -v $D/prog/s3.json:/etc/s3.json:ro chrislusf/seaweedfs:latest server -s3 -s3.config=/etc/s3.json >/dev/null || exit 2
trap 'docker rm -f $NAME >/dev/null 2>&1' EXIT
i=0; until curl -s -o /dev/null http://127.0.0.1:18333/; do i=$((i+1)); [ $i -gt 60 ] && { echo "seaweedfs did not start"; docker logs $NAME | tail -20; exit 2; }; sleep 1; done
sleep 3
docker image inspect chrislusf/seaweedfs:latest --format 'seaweedfs image {{.Id}} created {{.Created}}'
cd $D/prog && S3_ENDPOINT=127.0.0.1:18333 go run .
