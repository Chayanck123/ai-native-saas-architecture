#!/usr/bin/env bash
# Copy the service images built for one commit from GitHub's registry into this
# environment's ECR. Staging and production receive the very same image
# layers; nothing is rebuilt between environments.
#
#   promote-images.sh <git-sha> <source-prefix> <ecr-registry>
#   promote-images.sh 3f2c9e1 ghcr.io/acme/ainative 123456789012.dkr.ecr.us-east-1.amazonaws.com
set -euo pipefail

sha=$1 source=$2 registry=$3

for service in gateway knowledge agents billing growth; do
  target="$registry/ainative/$service:$sha"
  # ECR tags are immutable, so a re-run (or a rollback to this commit) reuses it.
  if aws ecr describe-images --repository-name "ainative/$service" \
      --image-ids "imageTag=$sha" >/dev/null 2>&1; then
    echo "$target already present"
    continue
  fi
  docker buildx imagetools create --tag "$target" "$source/$service:$sha"
  echo "promoted $source/$service:$sha -> $target"
done
