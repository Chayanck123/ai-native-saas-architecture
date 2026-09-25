# EKS cluster for the containerised microservices, with a managed node group and
# IAM Roles for Service Accounts (IRSA): each Kubernetes service account gets its
# own least-privilege AWS role - no shared node credentials.

variable "name" {
  type = string
}

variable "kubernetes_version" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "node_instance_types" {
  type = list(string)
}

variable "node_min_size" {
  type = number
}

variable "node_desired_size" {
  type = number
}

variable "node_max_size" {
  type = number
}

variable "service_accounts" {
  description = "Kubernetes service accounts that get an IRSA role."
  type        = list(string)
}

variable "namespace" {
  type = string
}

variable "service_role_prefix" {
  description = "Prefix of the services' IRSA role names (infra/k8s/base annotates ainative-<service>)."
  type        = string
}

variable "cluster_admin_role_arns" {
  description = "IAM roles (e.g. the CI/CD roles) granted cluster-admin through EKS access entries."
  type        = map(string)
  default     = {}
}

variable "secret_arns" {
  description = "Secrets Manager ARNs readable by the External Secrets operator."
  type        = list(string)
}

variable "documents_bucket" {
  description = "ARN of the document bucket the knowledge service may use."
  type        = string
}

data "aws_iam_policy_document" "cluster_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${var.name}-eks-cluster"
  assume_role_policy = data.aws_iam_policy_document.cluster_assume.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_kms_key" "secrets" {
  description         = "${var.name} EKS secrets envelope encryption"
  enable_key_rotation = true
}

resource "aws_eks_cluster" "this" {
  name     = var.name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = true
  }

  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = aws_kms_key.secrets.arn
    }
  }

  # Kubernetes access is granted with EKS access entries (IAM), not the aws-auth
  # ConfigMap. Whoever creates the cluster (the first, operator-run apply) is admin.
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator"]

  depends_on = [aws_iam_role_policy_attachment.cluster]
}

data "aws_iam_policy_document" "node_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${var.name}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])

  role       = aws_iam_role.node.name
  policy_arn = each.value
}

resource "aws_eks_node_group" "default" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "default"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.subnet_ids
  instance_types  = var.node_instance_types
  capacity_type   = "ON_DEMAND"

  scaling_config {
    min_size     = var.node_min_size
    desired_size = var.node_desired_size
    max_size     = var.node_max_size
  }

  update_config {
    max_unavailable = 1
  }

  depends_on = [aws_iam_role_policy_attachment.node]

  lifecycle {
    # The cluster autoscaler / Karpenter owns the running size.
    ignore_changes = [scaling_config[0].desired_size]
  }
}

locals {
  eks_addons = {
    "vpc-cni"    = { role = null, configuration = null }
    "coredns"    = { role = null, configuration = null }
    "kube-proxy" = { role = null, configuration = null }
    # CPU/memory metrics for the HorizontalPodAutoscalers.
    "metrics-server" = { role = null, configuration = null }
    # Persistent volumes (Prometheus); EKS >= 1.30 no longer has a default StorageClass.
    "aws-ebs-csi-driver" = {
      role          = aws_iam_role.addon["ebs-csi-controller"].arn
      configuration = jsonencode({ defaultStorageClass = { enabled = true } })
    }
  }
}

resource "aws_eks_addon" "this" {
  for_each = local.eks_addons

  cluster_name             = aws_eks_cluster.this.name
  addon_name               = each.key
  service_account_role_arn = each.value.role
  configuration_values     = each.value.configuration

  depends_on = [aws_eks_node_group.default]
}

# ---------------------------------------------------------- access entries
resource "aws_eks_access_entry" "admin" {
  for_each = var.cluster_admin_role_arns

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
}

resource "aws_eks_access_policy_association" "admin" {
  for_each = var.cluster_admin_role_arns

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_eks_access_entry.admin[each.key].principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}

# ------------------------------------------------------------------ IRSA
data "tls_certificate" "oidc" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "this" {
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.oidc.certificates[0].sha1_fingerprint]
}

locals {
  oidc_host = replace(aws_iam_openid_connect_provider.this.url, "https://", "")

  # Cluster add-ons that call AWS APIs, and their "<namespace>:<service account>".
  addon_service_accounts = {
    "external-secrets"             = "external-secrets:external-secrets"
    "aws-load-balancer-controller" = "kube-system:aws-load-balancer-controller"
    "ebs-csi-controller"           = "kube-system:ebs-csi-controller-sa"
    "otel-collector"               = "observability:otel-collector"
  }

  irsa_subjects = merge(
    { for name in var.service_accounts : name => "system:serviceaccount:${var.namespace}:${name}" },
    { for name, account in local.addon_service_accounts : name => "system:serviceaccount:${account}" },
  )
}

data "aws_iam_policy_document" "irsa_assume" {
  for_each = local.irsa_subjects

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.this.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = [each.value]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

# Staging and production live in separate AWS accounts, so these names match
# the eks.amazonaws.com/role-arn annotations in infra/k8s/base in both.
resource "aws_iam_role" "service" {
  for_each = toset(var.service_accounts)

  name               = "${var.service_role_prefix}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.irsa_assume[each.key].json
}

# Only the knowledge service may touch the document bucket.
data "aws_iam_policy_document" "knowledge" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${var.documents_bucket}/*"]
  }
}

resource "aws_iam_role_policy" "knowledge" {
  name   = "documents"
  role   = aws_iam_role.service["knowledge"].id
  policy = data.aws_iam_policy_document.knowledge.json
}

resource "aws_iam_role" "addon" {
  for_each = local.addon_service_accounts

  name               = "${var.name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.irsa_assume[each.key].json
}

data "aws_iam_policy_document" "external_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = var.secret_arns
  }
}

resource "aws_iam_role_policy" "external_secrets" {
  name   = "read-platform-secrets"
  role   = aws_iam_role.addon["external-secrets"].id
  policy = data.aws_iam_policy_document.external_secrets.json
}

# The upstream policy for this controller version (docs/install/iam_policy.json).
resource "aws_iam_policy" "load_balancer_controller" {
  name   = "${var.name}-aws-load-balancer-controller"
  policy = file("${path.module}/load-balancer-controller-policy.json")
}

resource "aws_iam_role_policy_attachment" "addon" {
  for_each = {
    "aws-load-balancer-controller" = aws_iam_policy.load_balancer_controller.arn
    "ebs-csi-controller"           = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
    "otel-collector"               = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
  }

  role       = aws_iam_role.addon[each.key].name
  policy_arn = each.value
}

output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "cluster_arn" {
  value = aws_eks_cluster.this.arn
}

output "cluster_endpoint" {
  value = aws_eks_cluster.this.endpoint
}

output "cluster_certificate_authority" {
  value = aws_eks_cluster.this.certificate_authority[0].data
}

output "node_security_group_id" {
  description = "Security group shared by the control plane and managed nodes."
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

output "service_role_arns" {
  value = { for name, role in aws_iam_role.service : name => role.arn }
}

output "addon_role_arns" {
  value = { for name, role in aws_iam_role.addon : name => role.arn }
}
