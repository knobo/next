#!/usr/bin/env bash
# Build the slides, push the image to git.knobo.no, roll it out at aiagent.knobo.no.
set -euo pipefail
cd "$(dirname "$0")"
K=(kubectl --context "${KUBE_CONTEXT:-default}")
TAG=$(date -u +%Y%m%d%H%M%S)
IMAGE=git.knobo.no/knobo/next-slides:$TAG
npm ci --no-audit --no-fund >/dev/null
npm run build >/dev/null
docker build -q -t "$IMAGE" . >/dev/null
docker push -q "$IMAGE" >/dev/null
# The registry pull secret, copied once from a namespace that already has it.
"${K[@]}" create namespace next-slides --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
"${K[@]}" -n next-slides get secret forgejo-registry >/dev/null 2>&1 || \
  "${K[@]}" -n admin-mcp get secret forgejo-registry -o json \
  | jq 'del(.metadata.namespace,.metadata.uid,.metadata.resourceVersion,.metadata.creationTimestamp,.metadata.ownerReferences,.metadata.annotations)' \
  | "${K[@]}" -n next-slides apply -f - >/dev/null
sed "s|IMAGE|$IMAGE|" k8s.yaml | "${K[@]}" apply -f -
"${K[@]}" -n next-slides rollout status deploy/slides --timeout=120s
echo "https://aiagent.knobo.no ($IMAGE)"
