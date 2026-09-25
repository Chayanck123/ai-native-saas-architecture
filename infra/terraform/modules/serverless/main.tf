# Serverless compute for spiky, short-lived work:
#   POST /webhooks/stripe  -> stripe-webhook Lambda   (payment events)
#   POST /t                -> telemetry-beacon Lambda (marketing-site analytics)
#   EventBridge Scheduler  -> scheduler Lambda        (cron jobs)
# Each function validates its input and publishes a CloudEvent to Redis.

variable "name" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "security_group_id" {
  type = string
}

variable "package_dir" {
  description = "Directory with the function code and its dependencies."
  type        = string
}

variable "redis_url_secret_arn" {
  type = string
}

variable "stripe_secret_arn" {
  type = string
}

variable "beacon_allowed_origin" {
  type = string
}

locals {
  functions = {
    "stripe-webhook"   = { handler = "serverless_functions.stripe_webhook.handler", timeout = 10, memory = 256 }
    "telemetry-beacon" = { handler = "serverless_functions.telemetry_beacon.handler", timeout = 5, memory = 256 }
    "scheduler"        = { handler = "serverless_functions.scheduler.handler", timeout = 30, memory = 256 }
  }
  schedules = {
    revenue_snapshot     = "cron(15 0 * * ? *)"
    pql_scoring          = "rate(1 hour)"
    experiment_autopilot = "rate(15 minutes)"
  }
}

data "archive_file" "functions" {
  type        = "zip"
  source_dir  = var.package_dir
  output_path = "${path.module}/.build/functions.zip"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "functions" {
  name               = "${var.name}-functions"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "vpc_access" {
  role       = aws_iam_role.functions.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy_attachment" "xray" {
  role       = aws_iam_role.functions.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.redis_url_secret_arn, var.stripe_secret_arn]
  }
}

resource "aws_iam_role_policy" "read_secrets" {
  name   = "read-secrets"
  role   = aws_iam_role.functions.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

resource "aws_cloudwatch_log_group" "functions" {
  for_each          = local.functions
  name              = "/aws/lambda/${var.name}-${each.key}"
  retention_in_days = 30
}

resource "aws_lambda_function" "this" {
  for_each = local.functions

  function_name    = "${var.name}-${each.key}"
  role             = aws_iam_role.functions.arn
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  handler          = each.value.handler
  filename         = data.archive_file.functions.output_path
  source_code_hash = data.archive_file.functions.output_base64sha256
  timeout          = each.value.timeout
  memory_size      = each.value.memory

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = [var.security_group_id]
  }

  environment {
    variables = {
      # Secrets are fetched from Secrets Manager at cold start, never stored in config.
      REDIS_URL_SECRET_ARN             = var.redis_url_secret_arn
      STRIPE_WEBHOOK_SECRET_SECRET_ARN = var.stripe_secret_arn
      REDIS_PREFIX                     = "ainative"
      BEACON_ALLOWED_ORIGIN            = var.beacon_allowed_origin
    }
  }

  tracing_config {
    mode = "Active"
  }

  depends_on = [aws_cloudwatch_log_group.functions]
}

# ------------------------------------------------------------ API Gateway
resource "aws_apigatewayv2_api" "public" {
  name          = "${var.name}-functions"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = [var.beacon_allowed_origin]
    allow_methods = ["POST", "OPTIONS"]
    allow_headers = ["content-type"]
    max_age       = 3600
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/apigateway/${var.name}-functions"
  retention_in_days = 30
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.public.id
  name        = "$default"
  auto_deploy = true

  # Edge throttling protects the functions (and Redis) from abusive traffic.
  default_route_settings {
    throttling_burst_limit = 500
    throttling_rate_limit  = 200
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn
    format = jsonencode({
      requestId = "$context.requestId"
      ip        = "$context.identity.sourceIp"
      route     = "$context.routeKey"
      status    = "$context.status"
      latency   = "$context.responseLatency"
    })
  }
}

resource "aws_apigatewayv2_integration" "this" {
  for_each = toset(["stripe-webhook", "telemetry-beacon"])

  api_id                 = aws_apigatewayv2_api.public.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.this[each.key].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "stripe" {
  api_id    = aws_apigatewayv2_api.public.id
  route_key = "POST /webhooks/stripe"
  target    = "integrations/${aws_apigatewayv2_integration.this["stripe-webhook"].id}"
}

resource "aws_apigatewayv2_route" "beacon" {
  api_id    = aws_apigatewayv2_api.public.id
  route_key = "POST /t"
  target    = "integrations/${aws_apigatewayv2_integration.this["telemetry-beacon"].id}"
}

resource "aws_lambda_permission" "api" {
  for_each = toset(["stripe-webhook", "telemetry-beacon"])

  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this[each.key].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.public.execution_arn}/*/*"
}

# ------------------------------------------------------------- Schedules
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "invoke_scheduler" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.this["scheduler"].arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "invoke-scheduler-function"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.invoke_scheduler.json
}

resource "aws_scheduler_schedule" "jobs" {
  for_each = local.schedules

  name                = "${var.name}-${replace(each.key, "_", "-")}"
  schedule_expression = each.value

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.this["scheduler"].arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ detail = { job = each.key } })

    retry_policy {
      maximum_retry_attempts = 3
    }
  }
}

output "api_endpoint" {
  value = aws_apigatewayv2_api.public.api_endpoint
}
