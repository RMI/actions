#!/usr/bin/env bash
# check for strings using git grep with clean annotations + summary.
# Always emits annotations *before* exiting nonzero.

set -euo pipefail

#  Parse args 
PATTERNS_INPUT=""
PATTERNS_FILE=""
MODE="regex"
IGNORE_CASE="true"
INCLUDE_INPUT=""
EXCLUDE_INPUT=""
MAX_ANN="${MAX_ANNOTATIONS:-200}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --patterns)        PATTERNS_INPUT="$2"; shift 2 ;;
    --patterns-file)   PATTERNS_FILE="$2"; shift 2 ;;
    --mode)            MODE="$2"; shift 2 ;;
    --ignore-case)     IGNORE_CASE="$2"; shift 2 ;;
    --include)         INCLUDE_INPUT="$2"; shift 2 ;;
    --exclude)         EXCLUDE_INPUT="$2"; shift 2 ;;
    --max-annotations) MAX_ANN="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

#  Build patterns file 
TMP_PATTERNS=".forbidden-patterns.txt"
: > "$TMP_PATTERNS"

if [[ -n "$PATTERNS_FILE" && -f "$PATTERNS_FILE" ]]; then
  cat "$PATTERNS_FILE" >> "$TMP_PATTERNS"
fi
if [[ -n "$PATTERNS_INPUT" ]]; then
  printf "%s\n" "$PATTERNS_INPUT" >> "$TMP_PATTERNS"
fi

# Normalize: drop empty lines and comments
# (Use awk to avoid sed -r portability issues)
awk 'BEGIN{RS="\n";FS="\n"} /^[[:space:]]*($|#)/{next} {print}' "$TMP_PATTERNS" > "${TMP_PATTERNS}.clean"
mv "${TMP_PATTERNS}.clean" "$TMP_PATTERNS"

if [[ ! -s "$TMP_PATTERNS" ]]; then
  echo "No patterns specified. Nothing to scan."
  exit 0
fi

#  git grep flags 
FLAGS="-nI --column"
if [[ "$MODE" == "literal" ]]; then
  FLAGS="$FLAGS -F"
else
  FLAGS="$FLAGS -E"
fi
if [[ "$IGNORE_CASE" == "true" ]]; then
  FLAGS="$FLAGS -i"
fi

#  Pathspecs 
# Parse include/exclude robustly (no phantom empty element)
readarray -t INC < <(printf '%s\n' "$INCLUDE_INPUT" | tr -d '\r' | awk 'NF')
readarray -t EXC_RAW < <(printf '%s\n' "$EXCLUDE_INPUT" | tr -d '\r' | awk 'NF')
EXC=(); for e in "${EXC_RAW[@]}"; do EXC+=(":(exclude)$e"); done

#  Run scan (never let grep's code abort before we annotate) 
set +e
GIT_GREP_CMD=(git grep $FLAGS -f "$TMP_PATTERNS")
if ((${#INC[@]})) || ((${#EXC[@]})); then
  GIT_GREP_CMD+=("--" "${INC[@]}" "${EXC[@]}")
else
  GIT_GREP_CMD+=("--" ".")
fi

echo "Running scan: ${GIT_GREP_CMD[*]}"
echo "Patterns:"
cat "$TMP_PATTERNS"

"${GIT_GREP_CMD[@]}" > .matches.txt 2> .grep.err
RC=$?
set -e

#  Inspect Scan Exit condition 
# git-grep exit codes: 0=match, 1=no match, 128+=error. :contentReference[oaicite:1]{index=1}
if [[ $RC -ge 128 ]]; then
  echo "git grep failed:"
  cat .grep.err >&2
  # Try to at least surface a helpful error annotation.
  echo "::error title=forbidden-patterns::git grep failed (exit $RC). See logs."
  exit $RC
fi

if [[ $RC -eq 1 ]]; then
  echo "No forbidden patterns found."
  exit 0
fi

#  We have matches: annotate + summary, then fail 
# GitHub annotation limits exist per step/job, so cap them. :contentReference[oaicite:2]{index=2}
count=0
while IFS= read -r line; do
  # Format: path:line:col:content (with --column)
  file="${line%%:*}"
  rest="${line#*:}"
  lineno="${rest%%:*}"
  rest2="${rest#*:}"
  # If column present, strip it
  if [[ "$rest2" =~ ^([0-9]+):(.*)$ ]]; then
    col="${BASH_REMATCH[1]}"
    snippet="${BASH_REMATCH[2]}"
  else
    col="1"
    snippet="$rest2"
  fi
  # Trim to avoid control chars
  snippet="${snippet//$'\r'/}"
  echo "::error file=$file,line=$lineno,col=$col,title=Forbidden pattern::$snippet"
  count=$((count+1))
  if [[ $count -ge $MAX_ANN ]]; then
    echo "::notice title=forbidden-patterns::Annotation cap ($MAX_ANN) reached; see summary for full list."
    break
  fi
done < .matches.txt

# Also write a clean summary table (GFM) for all matches. :contentReference[oaicite:3]{index=3}
{
  echo "### Forbidden pattern matches"
  echo
  echo "| File | Line | Snippet |"
  echo "|------|------|---------|"
  # Escape pipes minimally
  awk -F: '{
    file=$1; line=$2;
    sub(/^[^:]*:[^:]*:/,"",$0); # drop file:line:
    gsub(/\r/,"",$0);
    gsub(/\|/,"\\|",$0);
    printf("| %s | %s | %s |\n", file, line, $0);
  }' .matches.txt
} >> "$GITHUB_STEP_SUMMARY"

exit 1
