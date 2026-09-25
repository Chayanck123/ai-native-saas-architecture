# Runbook

How to set up, deploy, operate and debug the platform. Every alert in
[`alerts.yml`](../infra/observability/alerts.yml) links to a section of this page.

Commands assume production, whose Kubernetes namespace is `ainative`. For
staging, use `ainative-staging`.

- [First-time setup](#first-time-setup)
- [Deploying and rolling back](#deploying-and-rolling-back)
- [Everyday operations](#everyday-operations)
- [Alerts](#alerts)

## First-time setup

Staging and production each live in their **own AWS account**. Resource names
such as `ainative/platform` and `ainative-gateway` are the same in both.
Repeat these steps per environment; examples use staging.

### 1. Prerequisites

- An AWS account per environment, with admin access for the first apply.
- A domain for the public API (for example `api.staging.example.com`), with an
  **ACM certificate** for it in the same region. The load balancer controller
  finds the certificate from the Ingress host.
- Tools: `uv`, Terraform ≥ 1.11, the AWS CLI, `kubectl`, `jq`.

### 2. Terraform state bucket (once per account)

```bash
aws s3api create-bucket --bucket ainative-terraform-state-<account-id> --region us-east-1
aws s3api put-bucket-versioning --bucket ainative-terraform-state-<account-id> \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket ainative-terraform-state-<account-id> \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

State is encrypted and locked by S3 itself (`use_lockfile`), so no DynamoDB
table is needed.

### 3. First apply (by an operator)

Check `infra/terraform/environments/staging.tfvars`, especially
`github_repository` (exact casing) and `beacon_allowed_origin` (your
marketing site). Then:

```bash
make package-lambdas                  # the Lambda bundle Terraform uploads
cd infra/terraform
cp environments/staging.backend.hcl.example environments/staging.backend.hcl   # fill in the bucket
terraform init -backend-config=environments/staging.backend.hcl
terraform apply -var-file=environments/staging.tfvars
```

The operator who creates the cluster becomes its first admin. The same apply
creates the GitHub roles and gives them cluster access, so every later apply
can run from CI. Useful outputs:

```bash
terraform output github_environments    # variables for step 6
terraform output public_functions_url   # Stripe webhook base URL (step 5)
```

### 4. Third-party secrets

Terraform creates every secret. Generated ones (database passwords, JWT keys,
Redis) are filled in automatically; the API keys and the agent's model id start
as `set-me`. Set them while keeping each secret's other fields:

```bash
set_secret_field() {   # <secret-id> <field> <value>
  aws secretsmanager get-secret-value --secret-id "$1" --query SecretString --output text \
    | jq --arg field "$2" --arg value "$3" '.[$field] = $value' \
    | aws secretsmanager put-secret-value --secret-id "$1" --secret-string file:///dev/stdin
}
set_secret_field ainative/agents ANTHROPIC_API_KEY "sk-ant-..."
set_secret_field ainative/agents AGENTS_MODEL "<anthropic-model-id>"   # the model the agent runs on
set_secret_field ainative/knowledge VOYAGE_API_KEY "pa-..."
set_secret_field ainative/stripe STRIPE_WEBHOOK_SECRET "whsec_..."   # from step 5
```

Terraform ignores later changes to these values, so it never overwrites them.

### 5. Stripe

In the Stripe dashboard, add a webhook endpoint at
`<public_functions_url>/webhooks/stripe` for `customer.subscription.created`,
`customer.subscription.updated`, `customer.subscription.deleted`,
`invoice.paid` and `invoice.payment_failed`. Store its signing secret as shown
above. Create Checkout sessions with `metadata.tenant_id` and `metadata.plan`
(or prices with lookup keys like `pro_monthly`), so events map to workspaces.

### 6. GitHub environments

In **Settings → Environments**, create four environments. Add the variables
from `terraform output github_environments`:

| Environment | Protection | Variables |
| --- | --- | --- |
| `staging` | none | `AWS_REGION`, `AWS_ROLE_ARN`, `EKS_CLUSTER`, `PUBLIC_API_URL` |
| `production` | required reviewers; `main` only | same, for production |
| `staging-infra` | `main` only | `AWS_REGION`, `AWS_ROLE_ARN`, `TF_STATE_BUCKET` |
| `production-infra` | required reviewers; `main` only | same, for production |

The AWS roles trust only these environments, so no AWS keys are stored in
GitHub. Protect `main` so pull requests must pass the **CI** checks.

### 7. Kubernetes overlay and DNS

Set the public host in `infra/k8s/overlays/<env>/kustomization.yaml` (the
Ingress patch) and merge. Once the first deploy has created the load balancer,
point DNS at it:

```bash
kubectl get ingress public-api -n ainative -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
```

### 8. Turn on deployments and deploy

Until now the pipelines skip every AWS step, so a fresh repository stays green.
In **Settings → Secrets and variables → Actions → Variables**, add the
repository variable `ENABLE_AWS_DEPLOYMENTS` with the value `true`. Then merge
to `main`, or run **CD** from the Actions tab. The pipeline builds the images,
bootstraps each service's database and login, and rolls out.

## Deploying and rolling back

- **Services**: every merge to `main` runs CI, builds the images once, deploys
  them to staging, and waits for approval on the `production` environment. It
  then promotes the same images, with the gateway as a canary: 20%, 40% and 80%
  of pods for 5 to 10 minutes each, analyzed by Prometheus.
- **Infrastructure and Lambda functions**: changes under `infra/terraform`,
  `services/serverless` or `packages/platform-core` trigger **Infrastructure**,
  which plans and applies staging and then, after approval, production. The
  plan is shown in the run summary.
- **Automatic safety nets**: a failed database bootstrap stops the deploy
  before anything changes. If a release is applied but doesn't become healthy,
  the pipeline rolls its Deployments back. A canary that fails analysis is
  aborted by Argo Rollouts, and the stable version keeps serving.

**Rolling back by hand:**

```bash
# Services: the previous revision of every Deployment
scripts/deploy/rollback.sh ainative
# The production gateway canary: abort a rollout in progress, or return to the previous version
kubectl argo rollouts abort gateway-api -n ainative
kubectl argo rollouts undo gateway-api -n ainative
```

For a durable rollback, `git revert` the change and merge. The pipeline then
deploys the reverted state like any other change.

## Everyday operations

**Where to look**

| Question | Tool |
| --- | --- |
| Is it healthy? | Grafana *Platform overview*; Prometheus alerts |
| Why was this request slow or failing? | Traces (X-Ray in AWS, Jaeger locally): search by the `x-trace-id` response header, or the `trace_id` in the logs |
| What happened for this customer? | Logs filtered by `tenant_id`; every line has `trace_id` and `request_id` |
| Is work stuck? | `GET /v1/admin/queues` (staff): ready, delayed, in-flight and dead tasks per queue |

**Dead letters and redrive** (staff credentials, `platform:admin`):

```bash
curl -H "authorization: Bearer $STAFF_TOKEN" $API/v1/admin/queues
curl -H "authorization: Bearer $STAFF_TOKEN" $API/v1/admin/queues/billing/dead-letters
curl -X POST -H "authorization: Bearer $STAFF_TOKEN" \
  $API/v1/admin/queues/billing/dead-letters/<letter-id>/redrive
```

Redriving is safe because tasks are idempotent: a task that partly succeeded
before failing won't repeat its effects.

**Rotating secrets.** Update the value in Secrets Manager. External Secrets
syncs it within an hour, or immediately with:

```bash
kubectl annotate externalsecret agents-secrets -n ainative force-sync="$(date +%s)" --overwrite
kubectl rollout restart deployment/agents-api deployment/agents-worker -n ainative
```

Database passwords are re-applied to the roles by the bootstrap Job on the
next deploy. The RDS admin password is rotated by RDS itself.

**Scaling.** API replicas follow CPU (HPA) and workers follow queue depth
(KEDA). Limits are in `infra/k8s/base/<service>.yaml`. Aurora capacity is set
by `aurora_min_acu` and `aurora_max_acu` in the tfvars.

**Customer operations.** Plan changes normally come from Stripe. Staff can
grant a workspace extra credits, for example after an incident, with
`POST /v1/billing/credits/grants` (`platform:admin`; the body names the
`tenant_id`, `credits`, `reason` and an `idempotency_key`). Every response
carries an `x-request-id` header; ask customers for it.

## Alerts

### Gateway error budget burn

**Meaning:** the public API's 5xx ratio is above 1.44% over both the last hour
and the last 5 minutes. At that rate, a 99.9% monthly SLO spends 2% of its
error budget per hour. **Customers are affected. This alert pages.**

1. In Grafana, check which routes return 5xx and since when. Correlate with
   the last deploy (Actions → CD).
2. If it started with a deploy, roll back (see above). A production canary
   should already have aborted itself.
3. Otherwise open a failing trace. A downstream service or dependency
   (PostgreSQL, Redis, the model API) is usually the cause. Check
   `kubectl get pods -n ainative` for crash loops and `readyz` failures.
4. Database saturation: check Aurora capacity units and connections in
   CloudWatch, and raise `aurora_max_acu` if it is pegged.

### High latency p95

**Meaning:** a service's p95 latency has been above 1 s for 10 minutes.

1. Find the slow route in Grafana, then the slow span in a trace.
2. If it is database-bound, look for missing indexes or saturation. If it is
   CPU-bound, check the HPA has headroom (`kubectl get hpa -n ainative`).
3. Agent runs are asynchronous and shouldn't cause this. If `/v1/agents`
   routes are slow, check the entitlement call to billing.

### Dead-lettered tasks

**Meaning:** background tasks exhausted their retries (5 attempts by default,
8 for webhooks). The work they represent (indexing a document, billing usage,
delivering a webhook) has **not happened**.

1. List them: `GET /v1/admin/queues/<service>/dead-letters`. Each entry has
   the task, its payload and the last error.
2. Fix the cause. A bug needs a fix deployed first; a dependency outage needs
   to have recovered.
3. Redrive each letter (`POST .../redrive`). Tasks are idempotent, so this is
   safe.
4. Webhook deliveries that fail against a customer's endpoint are expected.
   Contact the customer if one endpoint keeps failing.

### Queue backlog growing

**Meaning:** more than 1,000 tasks have waited in a queue for 10 minutes.

1. Check whether KEDA is scaling the worker (`kubectl get scaledobject,hpa -n
   ainative`) and whether it hit `maxReplicaCount`.
2. If workers are running but slow, look at task latency and a trace. The
   model API or embeddings provider may be throttling.
3. Raise `maxReplicaCount` for a lasting increase in load, but keep
   PostgreSQL connection limits and model rate limits in mind.

### Retry storm

**Meaning:** a service retries more than 5 tasks a second, which usually means
a dependency is failing.

1. The task logs show the error. Rate limits (429) from the model or embedding
   API, database errors or an unreachable internal service are the usual
   causes.
2. Backoff with jitter keeps the load bounded. If the dependency stays down,
   tasks dead-letter and can be redriven later; nothing is lost.

### LLM latency high

**Meaning:** model calls have had a p95 above 60 s for 15 minutes.

1. Check the provider's status page and the `llm_request_duration_seconds`
   panel by model.
2. Consider a lower `AGENTS_EFFORT` (the platform config), which trades some
   answer quality for speed. Runs keep working, just slower.

### Token spend spike

**Meaning:** the hourly token rate is 3× the same hour yesterday. **This is
cost.**

1. Find the tenants and runs responsible: `llm_tokens_total` by service, and
   agent-run logs by `tenant_id`.
2. A runaway integration or abusive workspace is limited by its plan's rate
   limits and credit quota. Tighten them or suspend the key
   (`DELETE /v1/api-keys/{key_id}`) if needed.
3. `AGENTS_TOKEN_BUDGET` caps any single run.

### Signups stopped

**Meaning:** no sign-ups for 6 hours. The acquisition funnel may be broken.

1. Try signing up yourself (`POST /v1/auth/signup`) and check its trace.
2. Check the marketing site's traffic in `GET /v1/analytics/live`. If page
   views stopped too, the problem is upstream: the site, DNS or the telemetry
   beacon Lambda (check its CloudWatch logs).
3. Low-traffic periods (nights, holidays) can trigger this legitimately.
