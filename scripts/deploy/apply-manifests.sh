#!/usr/bin/env bash
# Render a Kustomize overlay with one commit's images and apply it.
#
#   apply-manifests.sh <overlay> <ecr-registry> <git-sha> <aws-account-id>
set -euo pipefail

overlay=$1 registry=$2 sha=$3 account=$4
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

# `kustomize edit` rewrites kustomization.yaml, so work on a copy.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -R "$root/infra/k8s" "$work/k8s"
cd "$work/k8s/overlays/$overlay"

for service in gateway knowledge agents billing growth; do
  kustomize edit set image "ainative/$service=$registry/ainative/$service:$sha"
done
# IRSA annotations name the account the environment runs in.
kustomize build . | sed "s/:ACCOUNT_ID:/:$account:/g" >"$work/manifests.yaml"

# Show what changes (exit code 1 means "there are differences").
kubectl diff -f "$work/manifests.yaml" || [ $? -eq 1 ]
kubectl apply -f "$work/manifests.yaml"
