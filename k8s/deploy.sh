#!/usr/bin/env bash
# Deploy the board to a Kubernetes cluster.
#
# board.py and board.css go in as a ConfigMap — no image build, no registry. The manifest
# carries ${BOARD_HOST}, ${BOARD_HUMAN} and ${NTFY_URL} as placeholders because those are
# per-installation values, not source code; they come from board.env (see
# board.env.example). That is also why `kubectl apply -f k8s/board.yaml` on its own is
# wrong: it would deploy the placeholders verbatim.
#
#   ./k8s/deploy.sh              deploy with board.env from the repo root
#   BOARD_ENV=path ./k8s/deploy.sh
#
# The board-policy ConfigMap is deliberately NOT applied here; see k8s/board.yaml.
set -euo pipefail
cd "$(dirname "$0")/.."

# board.env is untracked: `board task deploy` runs us from a clean origin/main worktree and
# passes BOARD_REPO_ROOT so it is still found in the primary checkout.
ENVFILE="${BOARD_ENV:-${BOARD_REPO_ROOT:-.}/board.env}"
[ -f "$ENVFILE" ] || {
  echo "no $ENVFILE — copy board.env.example and fill it in" >&2; exit 2; }
# shellcheck disable=SC1090
set -a; . "./$ENVFILE"; set +a

: "${BOARD_HOST:?set BOARD_HOST in $ENVFILE}"
: "${KUBE_CONTEXT:=}"
: "${BOARD_HUMAN:=human}"
: "${NTFY_URL:=}"
K=(kubectl ${KUBE_CONTEXT:+--context "$KUBE_CONTEXT"})

command -v envsubst >/dev/null || { echo "envsubst is missing (gettext)" >&2; exit 2; }

# The ConfigMap BEFORE the apply: the Deployment mounts it, and a pod that starts before
# the source is there crash-loops. board.css must be in it — board.py reads the stylesheet
# from its own directory, and without it the pages are unstyled.
"${K[@]}" -n board create configmap board-src \
  --from-file=board.py --from-file=board.css \
  --dry-run=client -o yaml | "${K[@]}" apply -f -

envsubst '${BOARD_HOST} ${BOARD_HUMAN} ${NTFY_URL}' < k8s/board.yaml | "${K[@]}" apply -f -
"${K[@]}" -n board rollout restart deploy/board
"${K[@]}" -n board rollout status deploy/board --timeout=120s
