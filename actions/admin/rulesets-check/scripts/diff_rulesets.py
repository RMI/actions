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
                                                       setting we track — real drift)

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


class Diff:
    def __init__(self):
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def walk(self, name: str, local, remote, path: str) -> None:
        """Local-driven recursive compare. Only keys in ``local`` are checked."""
        local = normalize_rules(local)
        remote = normalize_rules(remote)

        if isinstance(local, dict):
            if not isinstance(remote, dict):
                # We expect an object here but the remote has a scalar/other.
                self._mismatch(name, path, local, remote)
                return
            for key in sorted(local):
                child = f"{path}.{key}" if path else key
                rv = remote.get(key, MISSING)
                if rv is MISSING:
                    self._missing_on_remote(name, child, local[key])
                else:
                    self.walk(name, local[key], rv, child)
            return

        # Leaf: compare by value. Non-`rules` arrays are compared whole, matching
        # the wholesale merge semantics in resolve_local.py.
        if local != remote:
            self._mismatch(name, path, local, remote)

    def _mismatch(self, name: str, path: str, local, remote) -> None:
        self.failures.append(
            f"Ruleset '{name}': value mismatch at '{path}'\n"
            f"    local  = {json.dumps(local)}\n"
            f"    remote = {json.dumps(remote)}"
        )

    def _missing_on_remote(self, name: str, path: str, local_val) -> None:
        self.failures.append(
            f"Ruleset '{name}': tracked key '{path}' is missing from the live "
            f"ruleset — the remote does not enforce a setting we require\n"
            f"    local  = {json.dumps(local_val)}\n"
            f"    remote = (absent)"
        )


def emit_annotations(diff: Diff) -> None:
    for w in diff.warnings:
        print(f"::warning title=Ruleset not managed::{w.splitlines()[0]}")
    for f in diff.failures:
        print(f"::error title=Ruleset drift::{f.splitlines()[0]}")


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
                lines.append(f)
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
        print(
            "usage: diff_rulesets.py <local_dir> <remote_dir>",
            file=sys.stderr,
        )
        return 2

    local_dir = Path(sys.argv[1])
    remote_dir = Path(sys.argv[2])

    diff = Diff()

    local_files = {p.name: p for p in local_dir.glob("*.json")}
    remote_files = {p.name: p for p in remote_dir.glob("*.json")}

    # A ruleset we define but the remote lacks entirely -> fail.
    for only_local in sorted(set(local_files) - set(remote_files)):
        diff.failures.append(
            f"Ruleset '{only_local}' is defined locally but has no matching live "
            f"ruleset on the remote"
        )
        print(
            f"::error title=Ruleset missing::Ruleset '{only_local}' is defined "
            f"locally but not present on the remote"
        )

    # A ruleset on the remote we don't define -> informational (not our concern).
    unmanaged: list[str] = []
    for only_remote in sorted(set(remote_files) - set(local_files)):
        msg = (
            f"Ruleset '{only_remote}' exists on the remote but is not defined "
            f"locally — not managed by this check"
        )
        unmanaged.append(msg)

    for fname in sorted(set(local_files) & set(remote_files)):
        local = load_json(local_files[fname])
        remote = load_json(remote_files[fname])
        if local is None or remote is None:
            diff.failures.append(f"Ruleset '{fname}': could not parse one side")
            continue
        name = local.get("name", fname)
        diff.walk(name, local, remote, "")

    emit_annotations(diff)
    for u in unmanaged:
        print(f"::warning title=Ruleset not managed::{u}")
    write_summary(diff, unmanaged)

    print(
        f"\nSummary: {len(diff.failures)} drift failure(s), "
        f"{len(unmanaged)} unmanaged remote ruleset(s)."
    )
    return 1 if diff.failures else 0


if __name__ == "__main__":
    sys.exit(main())
