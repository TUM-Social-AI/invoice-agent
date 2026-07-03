# Infrastructure (AWS CDK, Python)

Provisions the scheduled **invoice-agent worker**: an EventBridge schedule fires a
Fargate task that runs `python -m src.orchestration.worker` (one poll), using DynamoDB
for dedup state, Secrets Manager for credentials, and CloudWatch for logs. It scales to
zero between polls.

```
EventBridge (rate) ─▶ Fargate task ─▶ Drive + LLM (outbound)
                          ├─▶ DynamoDB (dedup)
                          ├─▶ Secrets Manager (LLM key + Drive SA JSON)
                          └─▶ CloudWatch Logs
   minimal VPC · public subnet · egress-only SG · no NAT
```

## Prerequisites

- Node.js + AWS CDK CLI (`npm i -g aws-cdk`)
- Docker (only to build/push the worker image)
- AWS credentials for the target account, and a one-time `cdk bootstrap`
- Python deps: `pip install -r requirements.txt`

## Region

Defaults to **`eu-central-1` (Frankfurt)** — EU/GDPR, closest to the German dev team.
Override with `-c region=eu-west-1` (or `CDK_DEFAULT_REGION`).

## Deploy

```bash
cd infra
pip install -r requirements.txt

# 1. One-time per account/region
cdk bootstrap

# 2. Create the ECR repo + everything else
cdk deploy

# 3. Build & push the worker image to the ECR repo from step 2 (uses the repo root Dockerfile)
#    (EcrRepositoryUri is a stack output)
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=eu-central-1
REPO="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/invoice-agent"
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
docker build -t "$REPO:latest" ..        # build context = repo root
docker push "$REPO:latest"

# 4. Populate the secret values (never stored in code)
aws secretsmanager put-secret-value --secret-id invoice-agent/llm-api-key           --secret-string 'YOUR_GEMINI_OR_OPENAI_KEY'
aws secretsmanager put-secret-value --secret-id invoice-agent/drive-service-account --secret-string "$(cat service-account.json)"
```

The next scheduled tick launches the worker. Watch it in CloudWatch Logs under
`/invoice-agent/worker`, or trigger a run immediately from the ECS console.

## Configuration (CDK context)

| Key | Default | Purpose |
|-----|---------|---------|
| `region` | `eu-central-1` | Deploy region |
| `existingVpcId` | *(unset)* | Import an existing VPC instead of creating one |
| `scheduleRate` | `rate(10 minutes)` | EventBridge schedule expression |
| `llmProvider` | `gemini` | `LLM_PROVIDER` env for the task (`gemini`/`openai`/`ollama`) |
| `llmKeyEnvName` | `GOOGLE_API_KEY` | Env var the provider reads its key from (`OPENAI_API_KEY` for OpenAI) |
| `imageTag` | `latest` | ECR image tag to run |
| `dedupTableName` | `invoice-agent-dedup` | DynamoDB table name (keep aligned with `config/config.yaml`) |
| `cpu` / `memoryMib` | `2048` / `8192` | Fargate task size |

Example — deploy into an existing VPC, running OpenAI, every 5 minutes:

```bash
cdk deploy -c existingVpcId=vpc-0abc123 -c llmProvider=openai -c llmKeyEnvName=OPENAI_API_KEY -c scheduleRate="rate(5 minutes)"
```

## Notes / dependencies

- **Depends on the orchestration worker** (PR: `feat/orchestration-worker`) being in the
  image — the task command is `python -m src.orchestration.worker`.
- The **real Google Drive client** ships on a separate branch. Until it merges, the task
  will start and exit with a clear error (no `src.sources.google_drive`). The rest of the
  infra is unaffected.
- `dedupTableName` here and `orchestration.dynamodb_table` in `config/config.yaml` must
  match (both default to `invoice-agent-dedup`); `aws_region: null` in config lets the
  task use this stack's region automatically.
- Cost at low volume is dominated by LLM tokens; fixed AWS cost is ~zero (no NAT, DynamoDB
  on-demand, scale-to-zero compute).
