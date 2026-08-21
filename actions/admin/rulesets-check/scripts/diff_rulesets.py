#!/usr/bin/env python3
"""Diff resolved-local rulesets against the live remote rulesets.

Replaces the old ``diff --recursive`` step (PLAN.md §6.3). Uses an **allow-list**
model: the resolved template+overlay *is* the allow-list — only keys we actually
define are checked. This makes the per-PR check quiet and precise, and moves the
"is GitHub's schema fully covered" question to the nightly schema-coverage job
(scripts/schema_coverage.py).

The walk is **local-driven**. For every key present in local:

  * key in both, values differ (at a leaf) -> FAIL  (tracked value mismatch)
  * key in local but missing from remote    -> FAIL  (remote doesn't enforce a
                                                       setting we track — drift)

Keys present only in **remote** are ignored — a field GitHub returns that we
don't define is not our concern here (the nightly job flags genuinely new schema
properties). Volatile/identity fields (id, source, timestamps, ...) are never in
local, so they're auto-ignored — no strip list needed.

``rules`` is normalized to a ``{type: rule}`` map on both sides, so the walk is
order-independent and lines up by rule identity, not list index.

Ruleset-level:
  * a ruleset defined locally but absent on the remote -> FAIL
  * a ruleset present on the remote but not defined locally -> informational
    warning (we don't manage it; not a failure under the allow-list model)

Diagnostics: every failure prints its local/remote values to the step log and
into a hover-able annotation, and the offending ruleset's resolved-local + live
JSON are dumped in a collapsed ``::group::``. Re-running the job with debug
logging (RUNNER_DEBUG=1) dumps every ruleset, passing or not.

Exit code: 1 if any FAIL was recorded (after processing every ruleset, so one
bad ruleset doesn't hide others); 0 otherwise.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# A sentinel distinct from any JSON value (including None, which is meaningful).
MISSING = object()


def load_json(p: Path):
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        print(f"::error::invalid JSON in {p}: {e}", file=sys.stderr)
        return None


def normalize_rules(node):
    """Return a shallow copy of ``node`` with any ``rules`` list keyed by type.

    Applied at every dict level so nested comparisons line up by rule identity.
    """
    if not isinstance(node, dict):
        return node
    rules = node.get("rules")
    if isinstance(rules, list):
        keyed = {}
        for rule in rules:
            if isinstance(rule, dict) and "type" in rule:
                keyed[rule["type"]] = {
                    k: v for k, v in rule.items() if k != "type"
                }
            else:
                # Malformed rule; fall back to index key so it still surfaces.
                keyed[f"__index_{len(keyed)}"] = rule
        node = dict(node)
        node["rules"] = keyed
    return node


def _short(value, limit: int = 300) -> str:
    """One-line JSON, truncated for annotations (full value stays in the log)."""
    s = json.dumps(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


class Diff:
    def __init__(self):
        # Each failure is a structured record so it can be rendered for the log,
        # a concise annotation, and the step summary independently.
        self.failures: list[dict] = []

    def walk(self, name: str, local, remote, path: str) -> None:
        """Local-driven recursive compare. Only keys in ``local`` are checked."""
        local = normalize_rules(local)
        remote = normalize_rules(remote)

        if isinstance(local, dict):
            if not isinstance(remote, dict):
                self.failures.append(
                    {"kind": "mismatch", "name": name, "path": path or "(root)",
                     "local": local, "remote": remote}
                )
                return
            for key in sorted(local):
                child = f"{path}.{key}" if path else key
                rv = remote.get(key, MISSING)
                if rv is MISSING:
                    self.failures.append(
                        {"kind": "missing", "name": name, "path": child,
                         "local": local[key]}
                    )
                else:
                    self.walk(name, local[key], rv, child)
            return

        # Leaf: compare by value. Non-`rules` arrays are compared whole, matching
        # the wholesale merge semantics in resolve_local.py.
        if local != remote:
            self.failures.append(
                {"kind": "mismatch", "name": name, "path": path,
                 "local": local, "remote": remote}
            )


# --- rendering ---------------------------------------------------------------

def failure_full(f: dict) -> str:
    """Multi-line detail for the step log and summary."""
    if f["kind"] == "mismatch":
        return (
            f"Ruleset '{f['name']}': value mismatch at '{f['path']}'\n"
            f"    local  = {json.dumps(f['local'])}\n"
            f"    remote = {json.dumps(f['remote'])}"
        )
    if f["kind"] == "missing":
        return (
            f"Ruleset '{f['name']}': tracked key '{f['path']}' is missing from the "
            f"live ruleset — the remote does not enforce a setting we require\n"
            f"    local  = {json.dumps(f['local'])}\n"
            f"    remote = (absent)"
        )
    if f["kind"] == "ruleset_missing":
        return (
            f"Ruleset '{f['name']}' is defined locally but has no matching live "
            f"ruleset on the remote"
        )
    if f["kind"] == "parse":
        return f"Ruleset '{f['name']}': could not parse one side"
    return str(f)


def failure_oneline(f: dict) -> str:
    """Concise, value-bearing line for an annotation."""
    if f["kind"] == "mismatch":
        return (
            f"Ruleset '{f['name']}': {f['path']} differs — "
            f"local={_short(f['local'])} remote={_short(f['remote'])}"
        )
    if f["kind"] == "missing":
        return (
            f"Ruleset '{f['name']}': {f['path']} missing from live ruleset "
            f"(local={_short(f['local'])})"
        )
    if f["kind"] == "ruleset_missing":
        return f"Ruleset '{f['name']}' defined locally but absent on remote"
    if f["kind"] == "parse":
        return f"Ruleset '{f['name']}': could not parse one side"
    return str(f)


def emit_failures(diff: Diff) -> None:
    """Print full detail to the log and a concise annotation for each failure."""
    for f in diff.failures:
        # Full, human-readable detail in the step log (indented block).
        print(failure_full(f))
        # Concise, value-bearing annotation (surfaces on the Checks tab).
        print(f"::error title=Ruleset drift::{failure_oneline(f)}")


def dump_diagnostics(pairs: list[tuple], failing: set, full: bool) -> None:
    """Collapsed per-ruleset dump of resolved-local vs live JSON.

    Always dumps rulesets that failed; with RUNNER_DEBUG set, dumps all of them.
    """
    for name, local_obj, remote_obj in pairs:
        if not full and name not in failing:
            continue
        print(f"::group::diagnostics: {name}")
        print("----- resolved local (what we require) -----")
        print(json.dumps(local_obj, indent=2, sort_keys=True))
        print("----- live remote (raw from GitHub) -----")
        print(json.dumps(remote_obj, indent=2, sort_keys=True))
        print("::endgroup::")


def write_summary(diff: Diff, unmanaged: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = ["### Ruleset check", ""]

    if not diff.failures and not unmanaged:
        lines.append("✅ Every tracked key matches the live rulesets.")
    else:
        if diff.failures:
            lines.append(f"#### ❌ Drift (fail) — {len(diff.failures)}")
            lines.append("")
            for f in diff.failures:
                lines.append("```")
                lines.append(failure_full(f))
                lines.append("```")
            lines.append("")
        if unmanaged:
            lines.append(f"#### ⚠️ Rulesets on the remote we don't manage — {len(unmanaged)}")
            lines.append("")
            for u in unmanaged:
                lines.append(f"- {u}")
            lines.append("")

    with open(summary_path, "a") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: diff_rulesets.py <local_dir> <remote_dir>", file=sys.stderr)
        return 2

    local_dir = Path(sys.argv[1])
    remote_dir = Path(sys.argv[2])

    diff = Diff()

    local_files = {p.name: p for p in local_dir.glob("*.json")}
    remote_files = {p.name: p for p in remote_dir.glob("*.json")}

    # A ruleset we define but the remote lacks entirely -> fail.
    for only_local in sorted(set(local_files) - set(remote_files)):
        name = only_local[:-5] if only_local.endswith(".json") else only_local
        diff.failures.append({"kind": "ruleset_missing", "name": name})

    # A ruleset on the remote we don't define -> informational (not our concern).
    unmanaged: list[str] = []
    for only_remote in sorted(set(remote_files) - set(local_files)):
        unmanaged.append(
            f"Ruleset '{only_remote}' exists on the remote but is not defined "
            f"locally — not managed by this check"
        )

    pairs: list[tuple] = []
    for fname in sorted(set(local_files) & set(remote_files)):
        local = load_json(local_files[fname])
        remote = load_json(remote_files[fname])
        if local is None or remote is None:
            name = local.get("name", fname) if isinstance(local, dict) else fname
            diff.failures.append({"kind": "parse", "name": name})
            continue
        name = local.get("name", fname)
        pairs.append((name, local, remote))
        diff.walk(name, local, remote, "")

    emit_failures(diff)
    for u in unmanaged:
        print(f"::warning title=Ruleset not managed::{u}")

    # Diagnostics: dump the failing rulesets (or all, under RUNNER_DEBUG).
    failing = {f["name"] for f in diff.failures}
    dump_diagnostics(pairs, failing, bool(os.environ.get("RUNNER_DEBUG")))

    write_summary(diff, unmanaged)

    print(
        f"\nSummary: {len(diff.failures)} drift failure(s), "
        f"{len(unmanaged)} unmanaged remote ruleset(s)."
    )
    return 1 if diff.failures else 0


if __name__ == "__main__":
    sys.exit(main())
