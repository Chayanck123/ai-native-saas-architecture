# Secrets Manager entries, synced into Kubernetes by the External Secrets
# operator (see infra/k8s/base/platform.yaml). Generated secrets are created
# here; third-party API keys are placeholders that operators fill in once
# (Terraform then ignores their values so it never overwrites them).

variable "prefix" {
  type = string
}

variable "services" {
  type = list(string)
}

variable "redis_endpoint" {
  type = string
}

variable "redis_password" {
  type      = string
  sensitive = true
}

variable "aurora_host" {
  type = string
}

resource "random_password" "internal_jwt" {
  length  = 64
  special = false
}

resource "random_password" "user_jwt" {
  length  = 64
  special = false
}

# Each service gets its own database login (created by the bootstrap job).
resource "random_password" "database" {
  for_each = toset(var.services)
  length   = 40
  special  = false
}

resource "aws_secretsmanager_secret" "platform" {
  name = "${var.prefix}/platform"
}

resource "aws_secretsmanager_secret_version" "platform" {
  secret_id = aws_secretsmanager_secret.platform.id
  secret_string = jsonencode({
    REDIS_URL           = "rediss://:${var.redis_password}@${var.redis_endpoint}:6379/0"
    REDIS_ADDRESS       = "${var.redis_endpoint}:6379" # read by the KEDA queue scalers
    REDIS_PASSWORD      = var.redis_password
    INTERNAL_JWT_SECRET = random_password.internal_jwt.result
  })
}

resource "aws_secretsmanager_secret" "service" {
  for_each = toset(var.services)
  name     = "${var.prefix}/${each.key}"
}

resource "aws_secretsmanager_secret_version" "service" {
  for_each  = toset(var.services)
  secret_id = aws_secretsmanager_secret.service[each.key].id
  secret_string = jsonencode(merge(
    {
      DATABASE_URL = "postgresql+asyncpg://${each.key}:${random_password.database[each.key].result}@${var.aurora_host}:5432/${each.key}"
    },
    each.key == "gateway" ? { JWT_SECRET = random_password.user_jwt.result } : {},
    each.key == "agents" ? { ANTHROPIC_API_KEY = "set-me", AGENTS_MODEL = "set-me" } : {},
    each.key == "knowledge" ? { VOYAGE_API_KEY = "set-me" } : {},
  ))

  lifecycle {
    # API keys are set by an operator after the first apply.
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "stripe" {
  name = "${var.prefix}/stripe"
}

resource "aws_secretsmanager_secret_version" "stripe" {
  secret_id     = aws_secretsmanager_secret.stripe.id
  secret_string = jsonencode({ STRIPE_WEBHOOK_SECRET = "set-me" })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

output "platform_secret_arn" {
  value = aws_secretsmanager_secret.platform.arn
}

output "stripe_secret_arn" {
  value = aws_secretsmanager_secret.stripe.arn
}

output "secret_arns" {
  value = concat(
    [aws_secretsmanager_secret.platform.arn],
    [for secret in aws_secretsmanager_secret.service : secret.arn],
  )
}
