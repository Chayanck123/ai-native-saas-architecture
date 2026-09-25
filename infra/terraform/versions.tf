terraform {
  required_version = ">= 1.11" # S3 backend with native state locking (use_lockfile)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80, < 7.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = ">= 3.0, < 4.0"
    }
  }

  # Remote state with locking. Initialise with:
  #   terraform init -backend-config=environments/<env>.backend.hcl
  backend "s3" {}
}

# Cluster add-ons are installed through the EKS API as whoever runs Terraform
# (an operator for the first apply, then the CI role - both are cluster admins).
provider "helm" {
  kubernetes = {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.region]
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "ainative"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
