output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "aurora_endpoint" {
  value = module.data.aurora_endpoint
}

output "redis_endpoint" {
  value = module.data.redis_primary_endpoint
}

output "documents_bucket" {
  value = module.data.documents_bucket_name
}

output "ecr_repositories" {
  value = module.registry.repository_urls
}

output "public_functions_url" {
  description = "Base URL for Stripe webhooks (/webhooks/stripe) and the beacon (/t)."
  value       = module.serverless.api_endpoint
}

output "service_role_arns" {
  description = "IRSA role per service account (annotated in infra/k8s/base)."
  value       = module.eks.service_role_arns
}

output "github_environments" {
  description = "Variables to set on the GitHub environments the workflows deploy through."
  value = {
    (var.environment) = {
      AWS_REGION   = var.region
      AWS_ROLE_ARN = module.github.deploy_role_arn
      EKS_CLUSTER  = module.eks.cluster_name
    }
    "${var.environment}-infra" = {
      AWS_REGION   = var.region
      AWS_ROLE_ARN = module.github.terraform_role_arn
    }
  }
}
