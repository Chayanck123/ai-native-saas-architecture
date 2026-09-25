# Cluster add-ons the platform's Kubernetes manifests (infra/k8s) rely on,
# installed with Helm at pinned chart versions:
#
#   external-secrets              syncs AWS Secrets Manager into Kubernetes Secrets
#   cluster-secret-store          ... through this ClusterSecretStore (local chart)
#   keda                          scales workers on task-queue depth (ScaledObject)
#   argo-rollouts                 canary releases of the public gateway (Rollout)
#   aws-load-balancer-controller  the public Application Load Balancer (Ingress)
#   prometheus                    metrics, the SLO alerts, and canary analysis
#   opentelemetry-collector       receives the services' traces, exports to X-Ray

variable "cluster_name" {
  type = string
}

variable "region" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "role_arns" {
  description = "IRSA role per add-on (external-secrets, aws-load-balancer-controller, otel-collector)."
  type        = map(string)
}

variable "alert_rules" {
  description = "Prometheus alerting rules (YAML with a top-level `groups` list)."
  type        = string
}

locals {
  release_defaults = {
    atomic          = true # roll back a failed upgrade instead of leaving it half-applied
    cleanup_on_fail = true
    max_history     = 10
  }
}

# ------------------------------------------------------- load balancing
resource "helm_release" "aws_load_balancer_controller" {
  name            = "aws-load-balancer-controller"
  repository      = "https://aws.github.io/eks-charts"
  chart           = "aws-load-balancer-controller"
  version         = "3.5.0"
  namespace       = "kube-system"
  atomic          = local.release_defaults.atomic
  cleanup_on_fail = local.release_defaults.cleanup_on_fail
  max_history     = local.release_defaults.max_history

  values = [yamlencode({
    clusterName = var.cluster_name
    region      = var.region
    vpcId       = var.vpc_id
    # We only use Ingress. The Service webhook would make every Service creation
    # in the cluster fail whenever this controller is unavailable.
    enableServiceMutatorWebhook = false
    serviceAccount = {
      name        = "aws-load-balancer-controller"
      annotations = { "eks.amazonaws.com/role-arn" = var.role_arns["aws-load-balancer-controller"] }
    }
  })]
}

# ---------------------------------------------------------------- secrets
resource "helm_release" "external_secrets" {
  name             = "external-secrets"
  repository       = "https://charts.external-secrets.io"
  chart            = "external-secrets"
  version          = "2.11.0"
  namespace        = "external-secrets"
  create_namespace = true
  atomic           = local.release_defaults.atomic
  cleanup_on_fail  = local.release_defaults.cleanup_on_fail
  max_history      = local.release_defaults.max_history

  values = [yamlencode({
    installCRDs = true
    serviceAccount = {
      annotations = { "eks.amazonaws.com/role-arn" = var.role_arns["external-secrets"] }
    }
  })]
}

# Installed after the operator is ready, so its CRDs and admission webhook exist.
resource "helm_release" "cluster_secret_store" {
  name            = "cluster-secret-store"
  chart           = "${path.module}/charts/cluster-secret-store"
  namespace       = "external-secrets"
  atomic          = local.release_defaults.atomic
  cleanup_on_fail = local.release_defaults.cleanup_on_fail
  max_history     = local.release_defaults.max_history

  values = [yamlencode({ region = var.region })]

  depends_on = [helm_release.external_secrets]
}

# ------------------------------------------------------ scaling, delivery
resource "helm_release" "keda" {
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = "2.21.0"
  namespace        = "keda"
  create_namespace = true
  atomic           = local.release_defaults.atomic
  cleanup_on_fail  = local.release_defaults.cleanup_on_fail
  max_history      = local.release_defaults.max_history
}

resource "helm_release" "argo_rollouts" {
  name             = "argo-rollouts"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-rollouts"
  version          = "2.43.2"
  namespace        = "argo-rollouts"
  create_namespace = true
  atomic           = local.release_defaults.atomic
  cleanup_on_fail  = local.release_defaults.cleanup_on_fail
  max_history      = local.release_defaults.max_history
}

# ---------------------------------------------------------- observability
resource "helm_release" "prometheus" {
  name             = "prometheus"
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "prometheus"
  version          = "29.33.0"
  namespace        = "monitoring"
  create_namespace = true
  timeout          = 600 # the first install provisions EBS volumes
  atomic           = local.release_defaults.atomic
  cleanup_on_fail  = local.release_defaults.cleanup_on_fail
  max_history      = local.release_defaults.max_history

  values = [yamlencode({
    server = {
      # prometheus.monitoring.svc:9090 - the address the gateway's canary
      # analysis (infra/k8s/overlays/production) queries.
      fullnameOverride = "prometheus"
      service          = { servicePort = 9090 }
      retention        = "15d"
      persistentVolume = { size = "50Gi" }
      global = {
        scrape_interval     = "30s"
        evaluation_interval = "30s"
      }
    }
    # Pods are scraped via their prometheus.io/* annotations (the chart's default jobs).
    serverFiles = {
      "alerting_rules.yml" = yamldecode(var.alert_rules)
    }
    "prometheus-pushgateway" = { enabled = false }
  })]
}

resource "helm_release" "opentelemetry_collector" {
  name             = "otel-collector"
  repository       = "https://open-telemetry.github.io/opentelemetry-helm-charts"
  chart            = "opentelemetry-collector"
  version          = "0.173.1"
  namespace        = "observability"
  create_namespace = true
  atomic           = local.release_defaults.atomic
  cleanup_on_fail  = local.release_defaults.cleanup_on_fail
  max_history      = local.release_defaults.max_history

  values = [yamlencode({
    mode             = "deployment"
    replicaCount     = 2
    fullnameOverride = "otel-collector" # otel-collector.observability.svc:4318 (OTLP/HTTP)
    image = {
      repository = "ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib"
    }
    serviceAccount = {
      annotations = { "eks.amazonaws.com/role-arn" = var.role_arns["otel-collector"] }
    }
    # The services speak OTLP only.
    ports = {
      "jaeger-compact" = { enabled = false }
      "jaeger-thrift"  = { enabled = false }
      "jaeger-grpc"    = { enabled = false }
      zipkin           = { enabled = false }
    }
    config = {
      receivers = { jaeger = null, zipkin = null }
      exporters = {
        # Traces from the services join the Lambda functions' traces in AWS X-Ray.
        awsxray = { region = var.region }
      }
      service = {
        pipelines = {
          traces = {
            receivers  = ["otlp"]
            processors = ["memory_limiter", "batch"]
            exporters  = ["awsxray"]
          }
        }
      }
    }
  })]
}
