# Stateful services, all private and encrypted:
#   - Aurora PostgreSQL Serverless v2 (scales with load; pgvector for embeddings)
#   - ElastiCache Redis (event bus streams, task queues, idempotency, rate limits)
#   - S3 bucket for uploaded documents and exports

variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "aurora_min_acu" {
  type = number
}

variable "aurora_max_acu" {
  type = number
}

variable "aurora_instance_count" {
  type = number
}

variable "redis_node_type" {
  type = string
}

variable "deletion_protection" {
  type = bool
}

variable "allowed_security_groups" {
  description = "Security groups allowed to reach PostgreSQL and Redis."
  type        = list(string)
}

# ------------------------------------------------------------- PostgreSQL
resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
}

resource "aws_security_group" "postgres" {
  name        = "${var.name}-postgres"
  description = "Aurora PostgreSQL"
  vpc_id      = var.vpc_id

  ingress {
    description     = "PostgreSQL from workloads"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = var.allowed_security_groups
  }
}

# The newest PostgreSQL 16 minor release Aurora offers when the cluster is
# created; later minors arrive through RDS maintenance, not Terraform.
data "aws_rds_engine_version" "postgresql" {
  engine  = "aurora-postgresql"
  version = "16"
  latest  = true
}

resource "aws_rds_cluster" "this" {
  cluster_identifier          = var.name
  engine                      = "aurora-postgresql"
  engine_mode                 = "provisioned"
  engine_version              = data.aws_rds_engine_version.postgresql.version_actual
  master_username             = "platform_admin"
  manage_master_user_password = true # rotated by RDS, stored in Secrets Manager
  db_subnet_group_name        = aws_db_subnet_group.this.name
  vpc_security_group_ids      = [aws_security_group.postgres.id]
  storage_encrypted           = true
  backup_retention_period     = 14
  preferred_backup_window     = "03:00-04:00"
  copy_tags_to_snapshot       = true
  deletion_protection         = var.deletion_protection
  skip_final_snapshot         = !var.deletion_protection
  final_snapshot_identifier   = var.deletion_protection ? "${var.name}-final" : null

  serverlessv2_scaling_configuration {
    min_capacity = var.aurora_min_acu
    max_capacity = var.aurora_max_acu
  }

  lifecycle {
    ignore_changes = [engine_version]
  }
}

resource "aws_rds_cluster_instance" "this" {
  count = var.aurora_instance_count

  identifier                   = "${var.name}-${count.index}"
  cluster_identifier           = aws_rds_cluster.this.id
  instance_class               = "db.serverless"
  engine                       = aws_rds_cluster.this.engine
  engine_version               = aws_rds_cluster.this.engine_version
  performance_insights_enabled = true

  lifecycle {
    ignore_changes = [engine_version]
  }
}

# ------------------------------------------------------------------ Redis
resource "aws_elasticache_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
}

resource "aws_security_group" "redis" {
  name        = "${var.name}-redis"
  description = "ElastiCache Redis"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Redis from workloads"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = var.allowed_security_groups
  }
}

resource "random_password" "redis" {
  length  = 48
  special = false
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = var.name
  description                = "Event bus, task queues, idempotency keys and rate limits"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = 2 # primary + replica in another AZ
  automatic_failover_enabled = true
  multi_az_enabled           = true
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [aws_security_group.redis.id]
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.redis.result
  snapshot_retention_limit   = 7
}

# -------------------------------------------------------------- Documents
resource "aws_s3_bucket" "documents" {
  bucket_prefix = "${var.name}-documents-"
  force_destroy = !var.deletion_protection
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

output "aurora_endpoint" {
  value = aws_rds_cluster.this.endpoint
}

output "aurora_cluster_arn" {
  value = aws_rds_cluster.this.arn
}

output "aurora_master_secret_arn" {
  value = aws_rds_cluster.this.master_user_secret[0].secret_arn
}

output "redis_primary_endpoint" {
  value = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "redis_auth_token" {
  value     = random_password.redis.result
  sensitive = true
}

output "documents_bucket_arn" {
  value = aws_s3_bucket.documents.arn
}

output "documents_bucket_name" {
  value = aws_s3_bucket.documents.id
}
