# Org TRACE collector

Small HTTPS ingest service for a team: **org holds the S3 credentials**; laptops
only `POST` to this URL with an org-issued Bearer token.

```text
laptop hooks  --HTTPS+token-->  collector  --AWS creds-->  S3
                     \-- always JSONL locally on laptop (fail-open)
                     \-- durable outbox when collector is down
```

## What it accepts

| Method | Path | Auth | Body |
| --- | --- | --- | --- |
| `GET` | `/healthz` | none | (empty) |
| `POST` | `/v1/ingest` | `Authorization: Bearer <COLLECTOR_TOKEN>` | JSON object |

Ingest kinds:

```json
{"kind":"event","conversation_id":"…","body":{ /* one redacted hook record */ }}
{"kind":"batch","events":[
  {"kind":"event","conversation_id":"…","body":{…},"filename":"optional.jsonl"},
  {"kind":"trace","conversation_id":"…","body":{…}}
]}
{"events":[ /* same as kind:batch; bare array also accepted */ ]}
{"kind":"trace","conversation_id":"…","body":{ /* signed TRACE */ }}
{"kind":"honesty","conversation_id":"…","body":{ /* honesty sidecar */ }}
{"kind":"jsonl","conversation_id":"…","body":"…full jsonl text…","filename":"optional.jsonl"}
```

Batch responses include `accepted` / `failed` counts plus per-item `results`
and `errors`. HTTP-level `ok: true` means the batch was processed (clients
requeue only on transport / non-2xx failure).

## Server env (org only)

| Key | Required | Meaning |
| --- | --- | --- |
| `COLLECTOR_TOKEN` | **yes** | Shared secret for `Authorization: Bearer` |
| `S3_BUCKET` | no | When set, mirror objects to S3 |
| `PREFIX` | no | Object prefix (default `cursor-agent-trace`) |
| `LOCAL_DIR` | no | Disk fallback / local store (default `./data`) |
| `HOST` / `PORT` | no | Bind (default `0.0.0.0:8787`) |

No AWS keys on laptops. If S3 is unset or fails, the collector still writes under
`LOCAL_DIR` so local tests and early deploys work without AWS.

## S3 credentials on the collector (pick one)

`boto3` uses the default AWS credential chain. Configure **one** of:

### 1. Access keys (dev / simple VM)

```bash
export S3_BUCKET=my-org-traces
export AWS_ACCESS_KEY_ID=…
export AWS_SECRET_ACCESS_KEY=…
export AWS_REGION=us-east-1
```

Prefer short-lived keys or an instance role in production.

### 2. EC2 / ECS instance or task role

Omit static keys. Attach an IAM role with `s3:PutObject` on
`arn:aws:s3:::BUCKET/PREFIX/*` to the EC2 instance profile or ECS task role.
Set only:

```bash
export S3_BUCKET=my-org-traces
export AWS_REGION=us-east-1
export PREFIX=cursor-agent-trace
```

### 3. IRSA / EKS (IAM Roles for Service Accounts)

On EKS, annotate the ServiceAccount with an IAM role. The AWS SDK picks up:

| Env / file | Source |
| --- | --- |
| `AWS_ROLE_ARN` | Injected by the IRSA webhook |
| `AWS_WEB_IDENTITY_TOKEN_FILE` | Path to the projected service-account JWT |
| `AWS_REGION` | Cluster / pod env |

Example pod env (usually auto-injected; do not hard-code the token):

```yaml
env:
  - name: COLLECTOR_TOKEN
    valueFrom: { secretKeyRef: { name: trace-collector, key: token } }
  - name: S3_BUCKET
    value: my-org-traces
  - name: PREFIX
    value: cursor-agent-trace
  - name: AWS_REGION
    value: us-east-1
  # IRSA webhook also injects:
  # AWS_ROLE_ARN=arn:aws:iam::123456789012:role/trace-collector
  # AWS_WEB_IDENTITY_TOKEN_FILE=/var/run/secrets/eks.amazonaws.com/serviceaccount/token
```

IAM trust policy must allow `sts:AssumeRoleWithWebIdentity` from the cluster
OIDC provider for that ServiceAccount. No long-lived access keys in the pod.

## Local run (disk only)

```bash
cd collector
export COLLECTOR_TOKEN=dev-token
export LOCAL_DIR=./data
python3 server.py
# other terminal:
curl -sS -X POST http://127.0.0.1:8787/v1/ingest \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{"kind":"batch","events":[{"kind":"event","conversation_id":"demo","body":{"hook_event_name":"stop"}}]}'
ls data/cursor-agent-trace/
```

From the package root: `python -m lib manual-e2e` (starts collector, POSTs
batch, simulates outage → outbox → replay).

## Laptop setup

Point engineers at the parent [Install](../README.md#install) flags
(`--collector-url`, `--collector-token`). No AWS on the laptop. Collector is
off unless `COLLECTOR_URL` is set; hooks fail-open with a durable outbox.

## Deploy one-pager

### Fly.io

```bash
fly apps create my-trace-collector   # once
fly secrets set COLLECTOR_TOKEN=… S3_BUCKET=… AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=… AWS_REGION=us-east-1
fly deploy --dockerfile Dockerfile
# attach a volume if you want durable LOCAL_DIR fallback:
# fly volumes create collector_data --size 1
```

`fly.toml` sketch:

```toml
app = "my-trace-collector"
primary_region = "iad"

[build]
  dockerfile = "Dockerfile"

[env]
  PORT = "8787"
  PREFIX = "cursor-agent-trace"
  LOCAL_DIR = "/data"

[http_service]
  internal_port = 8787
  force_https = true
  auto_stop_machines = false
```

### Render

1. New **Web Service** from this `collector/` folder (Docker).
2. Env: `COLLECTOR_TOKEN`, optional `S3_BUCKET` + AWS keys / region.
3. Health check path: `/healthz`.
4. Instance disk or S3 for persistence (ephemeral disk alone is fine for early local deploys).

### ECS / Fargate

1. Task definition: image from this Dockerfile; port `8787`.
2. Task role with `s3:PutObject` on `arn:aws:s3:::BUCKET/PREFIX/*` (prefer role
   over long-lived keys).
3. Env: `COLLECTOR_TOKEN`, `S3_BUCKET`, `PREFIX`, `AWS_REGION`.
4. ALB target group health check: `GET /healthz`.
5. TLS terminate at the load balancer; laptops use `https://…`.

### EKS + IRSA

1. Create IAM role trusted by the cluster OIDC provider for the collector
   ServiceAccount; allow `s3:PutObject` on the trace prefix.
2. Annotate the ServiceAccount:
   `eks.amazonaws.com/role-arn: arn:aws:iam::ACCOUNT:role/trace-collector`
3. Deploy the collector Deployment; IRSA injects `AWS_ROLE_ARN` +
   `AWS_WEB_IDENTITY_TOKEN_FILE`. Set `COLLECTOR_TOKEN`, `S3_BUCKET`,
   `AWS_REGION` (no access keys).
4. Expose via Ingress / Service with TLS; laptops POST with Bearer token.

### Docker Compose (local)

```yaml
services:
  collector:
    build: .
    ports: ["8787:8787"]
    environment:
      COLLECTOR_TOKEN: changeme
      # S3_BUCKET: my-org-traces
      # AWS_ACCESS_KEY_ID: …
      # AWS_SECRET_ACCESS_KEY: …
      # AWS_REGION: us-east-1
      LOCAL_DIR: /data
    volumes:
      - ./data:/data
```

## Ops notes

- Rotate `COLLECTOR_TOKEN` by updating the secret and re-running
  `python -m lib install-hooks --collector-token …` on laptops (or editing `agent-trace.env`).
- Keep allow/deny + signed TRACE on the laptop unchanged; the collector is only
  a sink for already-redacted events and signed records.
- Do not put AWS keys in `agent-trace.env` on engineer machines when using the
  collector path.
- Laptop batch defaults (`COLLECTOR_BATCH_SIZE=10`,
  `COLLECTOR_FLUSH_INTERVAL_SEC=2`, retry backoff) are written by the installer.
