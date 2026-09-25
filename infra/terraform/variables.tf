variable "environment" {
  description = "Deployment environment (staging or production)."
  type        = string

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "CIDR block of the VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "az_count" {
  description = "Availability zones to spread across (3 for production resilience)."
  type        = number
  default     = 3
}

variable "single_nat_gateway" {
  description = "One shared NAT gateway (cheaper, for staging) instead of one per AZ."
  type        = bool
  default     = false
}

variable "kubernetes_version" {
  description = "EKS control plane version."
  type        = string
  default     = "1.35"
}

variable "node_instance_types" {
  description = "Instance types for the EKS managed node group."
  type        = list(string)
  default     = ["m7i.large"]
}

variable "node_min_size" {
  type    = number
  default = 3
}

variable "node_desired_size" {
  type    = number
  default = 3
}

variable "node_max_size" {
  type    = number
  default = 12
}

variable "aurora_min_acu" {
  description = "Aurora Serverless v2 minimum capacity (ACUs)."
  type        = number
  default     = 0.5
}

variable "aurora_max_acu" {
  description = "Aurora Serverless v2 maximum capacity (ACUs)."
  type        = number
  default     = 16
}

variable "aurora_instance_count" {
  description = "Writer plus readers."
  type        = number
  default     = 2
}

variable "redis_node_type" {
  type    = string
  default = "cache.r7g.large"
}

variable "deletion_protection" {
  description = "Protect stateful resources from accidental deletion."
  type        = bool
  default     = true
}

variable "lambda_package_dir" {
  description = "Directory containing the built Lambda package (see `make package-lambdas`)."
  type        = string
  default     = "build/functions"
}

variable "beacon_allowed_origin" {
  description = "Origin allowed to call the telemetry beacon (the marketing site)."
  type        = string
  default     = "https://www.example.com"
}

variable "github_repository" {
  description = "GitHub repository (owner/name, exact casing) whose workflows may deploy here."
  type        = string
}
