#!/usr/bin/env bash
# Run the database bootstrap Job (infra/k8s/jobs/db-bootstrap.yaml) inside the
# cluster - the databases are private to the VPC - and wait for it to finish.
#
#   bootstrap-databases.sh <namespace> <image> <database-cluster-id> <run-id>
set -euo pipefail

namespace=$1 image=$2 cluster=$3 run_id=$4
name="db-bootstrap-$run_id"
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

database=$(aws rds describe-db-clusters --db-cluster-identifier "$cluster" \
  --query 'DBClusters[0].[Endpoint,MasterUserSecret.SecretArn]' --output text)
read -r db_host admin_secret_arn <<<"$database"
if [[ -z $admin_secret_arn || $admin_secret_arn == None ]]; then
  echo "database cluster $cluster has no RDS-managed admin secret" >&2
  exit 1
fi
export NAMESPACE=$namespace IMAGE=$image RUN_ID=$run_id
export DB_HOST=$db_host DB_ADMIN_SECRET_ARN=$admin_secret_arn

# The Secret holding the admin credentials goes away with its ExternalSecret.
trap 'kubectl delete externalsecret "$name" -n "$namespace" --ignore-not-found --wait=false' EXIT

kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f -
# shellcheck disable=SC2016 # envsubst expands only the variables listed here
envsubst '${NAMESPACE} ${IMAGE} ${RUN_ID} ${DB_HOST} ${DB_ADMIN_SECRET_ARN}' \
  <"$root/infra/k8s/jobs/db-bootstrap.yaml" | kubectl apply -f -

kubectl wait "externalsecret/$name" -n "$namespace" --for=condition=Ready --timeout=2m

for _ in $(seq 120); do
  conditions=" $(kubectl get job "$name" -n "$namespace" \
    -o jsonpath='{.status.conditions[?(@.status=="True")].type}') "
  case $conditions in
    *" Complete "*)
      kubectl logs "job/$name" -n "$namespace"
      exit 0
      ;;
    *" Failed "*)
      kubectl logs "job/$name" -n "$namespace" || true
      echo "database bootstrap failed" >&2
      exit 1
      ;;
  esac
  sleep 5
done
echo "timed out waiting for job/$name" >&2
exit 1
