#!/usr/bin/env bash
# Return every Deployment in the namespace to its previous revision - used when
# a deploy was applied but did not become healthy. (Every deploy changes all
# image tags, so "previous" is the last deploy.) Canary Rollouts need nothing:
# Argo Rollouts already aborted them and kept the stable version serving.
#
#   rollback.sh <namespace>
set -euo pipefail

namespace=$1

for deployment in $(kubectl get deployments -n "$namespace" \
    -l app.kubernetes.io/part-of=ainative -o name); do
  kubectl rollout undo "$deployment" -n "$namespace"
done
for deployment in $(kubectl get deployments -n "$namespace" \
    -l app.kubernetes.io/part-of=ainative -o name); do
  kubectl rollout status "$deployment" -n "$namespace" --timeout=10m
done
