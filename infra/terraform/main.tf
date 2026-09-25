# AWS footprint of the platform:
#
#   network     VPC across 3 AZs, public/private subnets, NAT
#   eks         Kubernetes for the containerised microservices (+ IRSA roles)
#   addons      Helm-installed cluster add-ons the Kubernetes manifests rely on
#   data        Aurora PostgreSQL Serverless v2 (pgvector), ElastiCache Redis, S3
#   registry    ECR repositories for the service images
#   secrets     Secrets Manager entries synced into Kubernetes by External Secrets
#   serverless  Lambda functions behind API Gateway + EventBridge schedules
#   github      OIDC roles for the CI/CD workflows (no long-lived AWS keys)

locals {
  name     = "ainative-${var.environment}"
  services = ["gateway", "knowledge", "agents", "billing", "growth"]
}

module "network" {
  source = "./modules/network"

  name               = local.name
  cidr               = var.vpc_cidr
  az_count           = var.az_count
  single_nat_gateway = var.single_nat_gateway
}

module "eks" {
  source = "./modules/eks"

  name                = local.name
  kubernetes_version  = var.kubernetes_version
  vpc_id              = module.network.vpc_id
  subnet_ids          = module.network.private_subnet_ids
  node_instance_types = var.node_instance_types
  node_min_size       = var.node_min_size
  node_desired_size   = var.node_desired_size
  node_max_size       = var.node_max_size
  service_accounts    = local.services
  service_role_prefix = "ainative"
  namespace           = var.environment == "production" ? "ainative" : "ainative-${var.environment}"
  # The database bootstrap job also reads the RDS-managed admin secret.
  secret_arns      = concat(module.secrets.secret_arns, [module.data.aurora_master_secret_arn])
  documents_bucket = module.data.documents_bucket_arn
  cluster_admin_role_arns = {
    github-deploy    = module.github.deploy_role_arn
    github-terraform = module.github.terraform_role_arn
  }
}

module "addons" {
  source = "./modules/addons"

  cluster_name = module.eks.cluster_name
  region       = var.region
  vpc_id       = module.network.vpc_id
  role_arns    = module.eks.addon_role_arns
  alert_rules  = file("${path.root}/../observability/alerts.yml")

  # Needs running nodes, CoreDNS and the EBS CSI driver.
  depends_on = [module.eks]
}

module "data" {
  source = "./modules/data"

  name                    = local.name
  vpc_id                  = module.network.vpc_id
  subnet_ids              = module.network.private_subnet_ids
  aurora_min_acu          = var.aurora_min_acu
  aurora_max_acu          = var.aurora_max_acu
  aurora_instance_count   = var.aurora_instance_count
  redis_node_type         = var.redis_node_type
  deletion_protection     = var.deletion_protection
  allowed_security_groups = [module.eks.node_security_group_id, module.network.functions_security_group_id]
}

module "registry" {
  source = "./modules/registry"

  repositories = local.services
}

module "secrets" {
  source = "./modules/secrets"

  # Staging and production live in separate AWS accounts, so secret names can be
  # identical (and match infra/k8s/base) in both.
  prefix         = "ainative"
  services       = local.services
  redis_endpoint = module.data.redis_primary_endpoint
  redis_password = module.data.redis_auth_token
  aurora_host    = module.data.aurora_endpoint
}

module "github" {
  source = "./modules/github"

  name                 = local.name
  environment          = var.environment
  repository           = var.github_repository
  ecr_repository_arns  = values(module.registry.repository_arns)
  cluster_arn          = module.eks.cluster_arn
  database_cluster_arn = module.data.aurora_cluster_arn
}

module "serverless" {
  source = "./modules/serverless"

  name                  = local.name
  subnet_ids            = module.network.private_subnet_ids
  security_group_id     = module.network.functions_security_group_id
  package_dir           = "${path.root}/${var.lambda_package_dir}"
  redis_url_secret_arn  = module.secrets.platform_secret_arn
  stripe_secret_arn     = module.secrets.stripe_secret_arn
  beacon_allowed_origin = var.beacon_allowed_origin
}
