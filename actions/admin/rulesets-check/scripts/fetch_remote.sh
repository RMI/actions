#!/usr/bin/env bash
# Fetch the live rulesets for a repo and write one raw JSON file per ruleset,
# named by sanitized ruleset name. Field stripping (volatile / identity fields)
# is NOT done here — diff_rulesets.py strips both sides centrally from
# schema/diff-config.json, so there's a single source of truth.
#
# Usage: fetch_remote.sh <repo> <out_dir>
#   <repo>    owner/name (e.g. RMI/stitch) — passed to `gh api`
#   <out_dir> directory to write /<sanitized-name>.json into
#
# Requires: gh (authenticated via GH_TOKEN), jq.

set -euo pipefail

REPO="${1:?usage: fetch_remote.sh <repo> <out_dir>}"
OUT_DIR="${2:?usage: fetch_remote.sh <repo> <out_dir>}"

mkdir -p "$OUT_DIR"

echo "Fetching rulesets for ${REPO}"
ALL="$(gh api "/repos/${REPO}/rulesets")"

# IDs of every ruleset attached to the repo.
mapfile -t IDS < <(printf '%s' "$ALL" | jq -r '.[].id')

if ((${#IDS[@]} == 0)); then
  echo "No rulesets found on ${REPO}."
  exit 0
fi

for ID in "${IDS[@]}"; do
  DETAIL="$(gh api "/repos/${REPO}/rulesets/${ID}")"
  NAME="$(printf '%s' "$DETAIL" | jq -r '.name')"
  # Match resolve_local.py's sanitize(): keep [A-Za-z0-9._-], else '_'.
  # NOTE: '-' MUST be last in the set — inside tr, 'a.-_' would be read as the
  # range '.'..'_' (which excludes '-' itself and mangles hyphenated names).
  SAN="$(printf '%s' "$NAME" | tr -c 'a-zA-Z0-9._-' '_')"
  printf '%s\n' "$DETAIL" > "${OUT_DIR}/${SAN}.json"
  echo "  ${ID}  ${NAME}  ->  ${SAN}.json"
done

echo "Wrote ${#IDS[@]} remote ruleset(s) to ${OUT_DIR}"
