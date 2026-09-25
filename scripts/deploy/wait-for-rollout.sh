#!/usr/bin/env bash
# Wait until every workload in the namespace runs the new version. Canary
# Rollouts (production gateway) take longer: they pause between traffic steps
# and are aborted automatically if Prometheus reports errors or high latency.
#
#   wait-for-rollout.sh <namespace>
set -euo pipefail

namespace=$1

for deployment in $(kubectl get deployments -n "$namespace" \
    -l app.kubernetes.io/part-of=ainative -o name); do
  kubectl rollout status "$deployment" -n "$namespace" --timeout=10m
done

for rollout in $(kubectl get rollouts.argoproj.io -n "$namespace" -o name 2>/dev/null); do
  kubectl argo rollouts status "${rollout#*/}" -n "$namespace" --timeout 45m
done
