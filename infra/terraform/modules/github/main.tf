# Lets GitHub Actions deploy without long-lived AWS keys. A workflow job trades
# its GitHub-signed OIDC token for short-lived credentials of one of these
# roles, and only jobs running in the matching GitHub *environment* can: that
# is where approvals (e.g. required reviewers for production) are enforced.
#
#   deploy     .github/workflows/cd.yml     environment "<env>"
#              push images to ECR, look up the cluster and database, kubectl
#   terraform  .github/workflows/infra.yml  environment "<env>-infra"
#              plan and apply this configuration

variable "repository" {
  description = "GitHub repository allowed to assume the roles, as owner/name (exact casing)."
  type        = string
}

variable "environment" {
  type = string
}

variable "name" {
  description = "Prefix for the role names."
  type        = string
}

variable "ecr_repository_arns" {
  type = list(string)
}

variable "cluster_arn" {
  type = string
}

variable "database_cluster_arn" {
  type = string
}

locals {
  github_environments = {
    deploy    = var.environment
    terraform = "${var.environment}-infra"
  }
}

# One GitHub OIDC provider per AWS account (staging and production are separate accounts).
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "trust" {
  for_each = local.github_environments

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.repository}:environment:${each.value}"]
    }
  }
}

resource "aws_iam_role" "github" {
  for_each = local.github_environments

  name                 = "${var.name}-github-${each.key}"
  assume_role_policy   = data.aws_iam_policy_document.trust[each.key].json
  max_session_duration = 3600
}

# ------------------------------------------------------------------ deploy
data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "PushServiceImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = var.ecr_repository_arns
  }
  statement {
    sid       = "FindCluster"
    actions   = ["eks:DescribeCluster"]
    resources = [var.cluster_arn]
  }
  statement {
    # The database bootstrap job needs the Aurora endpoint and admin secret ARN.
    sid       = "FindDatabase"
    actions   = ["rds:DescribeDBClusters"]
    resources = [var.database_cluster_arn]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy"
  role   = aws_iam_role.github["deploy"].id
  policy = data.aws_iam_policy_document.deploy.json
}

# --------------------------------------------------------------- terraform
# Terraform manages VPCs, IAM, EKS and databases, so it needs broad rights.
# They are only reachable from the "<env>-infra" GitHub environment, which
# should require reviewers and allow only the main branch.
resource "aws_iam_role_policy_attachment" "terraform" {
  role       = aws_iam_role.github["terraform"].name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

output "deploy_role_arn" {
  value = aws_iam_role.github["deploy"].arn
}

output "terraform_role_arn" {
  value = aws_iam_role.github["terraform"].arn
}
