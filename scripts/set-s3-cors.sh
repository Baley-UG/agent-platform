#!/usr/bin/env bash
# Set the CORS rules the admin panel needs for direct browser PUTs
# (reference upload) against the Hetzner bucket. Bucket-level config —
# run once per bucket, idempotent. Requires the content-pipeline-api
# container (it carries boto3 + the S3 credentials).
#
#   ./scripts/set-s3-cors.sh
set -euo pipefail

docker exec -i agent-platform-content-pipeline-api-1 python <<'PY'
from app.core import s3 as s3lib
from app.core.config import settings

client = s3lib.client()
client.put_bucket_cors(
    Bucket=settings.S3_BUCKET,
    CORSConfiguration={
        "CORSRules": [
            {
                "AllowedOrigins": [
                    "https://agent-platform.internal.baley.eu",
                    "http://localhost:3100",
                    "http://localhost:3000",
                ],
                "AllowedMethods": ["PUT", "GET", "HEAD"],
                "AllowedHeaders": ["*"],
                "ExposeHeaders": ["ETag"],
                "MaxAgeSeconds": 3600,
            }
        ]
    },
)
print("CORS set on bucket:", settings.S3_BUCKET)
PY
