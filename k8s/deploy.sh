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
case "$ENVFILE" in */*) ;; *) ENVFILE="./$ENVFILE" ;; esac
# shellcheck disable=SC1090
set -a; . "$ENVFILE"; set +a

: "${BOARD_HOST:?set BOARD_HOST in $ENVFILE}"
: "${KUBE_CONTEXT:=}"
: "${BOARD_HUMAN:=human}"
: "${NTFY_URL:=}"
K=(kubectl ${KUBE_CONTEXT:+--context "$KUBE_CONTEXT"})

command -v envsubst >/dev/null || { echo "envsubst is missing (gettext)" >&2; exit 2; }

# The one that is left: a ConfigMap's data may total 1 MiB. Past it the apply fails with
# a message about the object, not about the file that grew, so say which file and by how
# much while we still know.
SRCBYTES=$(( $(wc -c < board.py) + $(wc -c < board.css) ))
[ "$SRCBYTES" -lt 1048576 ] || {
  echo "board.py + board.css is $SRCBYTES bytes — a ConfigMap holds 1048576." >&2
  echo "The board would have to ship as an image instead of as source." >&2; exit 2; }

# The ConfigMap BEFORE the apply: the Deployment mounts it, and a pod that starts before
# the source is there crash-loops. board.css must be in it — board.py reads the stylesheet
# from its own directory, and without it the pages are unstyled.
#
# --server-side, and that is not a preference. A client-side `apply` stores the ENTIRE
# object in the kubectl.kubernetes.io/last-applied-configuration annotation, and an
# annotation may not exceed 262144 bytes. board.py plus board.css passed that and the
# deploy stopped dead with "metadata.annotations: Too long" — the source itself was
# nowhere near the 1 MiB a ConfigMap may hold. Server-side apply keeps the state on the
# server and writes no such annotation, so the real limit is the real limit.
# --force-conflicts takes ownership of the fields the old client-side applies left behind;
# without it the first server-side apply over them is refused.
"${K[@]}" -n board create configmap board-src \
  --from-file=board.py --from-file=board.css \
  --dry-run=client -o yaml | "${K[@]}" apply --server-side --force-conflicts -f -

envsubst '${BOARD_HOST} ${BOARD_HUMAN} ${NTFY_URL}' < k8s/board.yaml | "${K[@]}" apply -f -
"${K[@]}" -n board rollout restart deploy/board
"${K[@]}" -n board rollout status deploy/board --timeout=120s
