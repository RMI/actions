#!/usr/bin/env bash
# Fetch the live rulesets for a repo and write one raw JSON file per ruleset,
# named by sanitized ruleset name. No field stripping — the allow-list diff
# (diff_rulesets.py) only compares keys the templates define, so volatile /
# identity fields (id, source, timestamps, ...) are ignored automatically.
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
# List all rulesets first, in a plain assignment so a failed `gh api` is caught
# by `set -e` and aborts. A process substitution (`mapfile < <(gh ...)`) would
# hide gh's exit code, making an auth/API error indistinguishable from "no
# rulesets" — the check would then pass vacuously or misreport every ruleset as
# missing. `gh api --paginate` walks all pages (this endpoint is paginated),
# emitting one JSON array per page; jq's streaming parser reads them all.
RULESETS_JSON="$(gh api --paginate "/repos/${REPO}/rulesets")"

mapfile -t IDS < <(printf '%s' "$RULESETS_JSON" | jq -r '.[].id')

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
