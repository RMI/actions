#!/usr/bin/env python3
"""Diff resolved-local rulesets against the live remote rulesets.

Replaces the old ``diff --recursive`` step (PLAN.md §6.3). Walks both structures
recursively and classifies at every level:

  * key in both, values differ (at a leaf) -> FAIL  (tracked value mismatch)
  * key only in remote                      -> WARN  (GitHub added a field)
  * key only in local                       -> WARN  (GitHub removed/renamed it)
  * ruleset present on only one side        -> WARN  (added/removed entirely)

Why recursive: GitHub adds/removes fields *inside* ``rules[].parameters`` and
``conditions``, not just at the top level, so a top-level-only comparison would
misclassify nested schema drift as a value mismatch and fail on it.

``rules`` is normalized to a ``{type: rule}`` map on both sides before comparing,
so the walk is order-independent and lines up by rule identity, not list index.

Exit code: 1 if any FAIL was recorded (after processing every ruleset, so one
bad ruleset doesn't hide others); 0 otherwise (warnings alone never fail).
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


def load_config(path: Path) -> "tuple[set, set]":
    """Load (strip, ignore) sets from the diff-config file.

    ``strip``  — top-level fields removed from both sides before comparing.
    ``ignore`` — dotted key paths whose new/removed-key drift is silenced.

    A bare list is accepted for backward compatibility and treated as ``ignore``.
    """
    if not path or not path.is_file():
        return set(), set()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return set(), set()
    if isinstance(data, list):
        return set(), set(data)
    if isinstance(data, dict):
        strip = data.get("strip", [])
        ignore = data.get("ignore", [])
        return (
            set(strip) if isinstance(strip, list) else set(),
            set(ignore) if isinstance(ignore, list) else set(),
        )
    return set(), set()


def strip_fields(node, fields: set):
    """Return ``node`` with any top-level key in ``fields`` removed."""
    if not isinstance(node, dict) or not fields:
        return node
    return {k: v for k, v in node.items() if k not in fields}


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
    def __init__(self, ignore: set):
        self.ignore = ignore
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def _ignored(self, path: str) -> bool:
        return path in self.ignore

    def walk(self, name: str, local, remote, path: str) -> None:
        local = normalize_rules(local)
        remote = normalize_rules(remote)

        if isinstance(local, dict) and isinstance(remote, dict):
            for key in sorted(set(local) | set(remote)):
                child = f"{path}.{key}" if path else key
                lv = local.get(key, MISSING)
                rv = remote.get(key, MISSING)
                if lv is MISSING:
                    self._new_key(name, child)
                elif rv is MISSING:
                    self._removed_key(name, child)
                else:
                    self.walk(name, lv, rv, child)
            return

        # Leaf (or type mismatch, or non-dict container): compare by value.
        # Non-`rules` arrays are compared as whole values (wholesale semantics),
        # matching how they're merged.
        if local != remote:
            self.failures.append(
                f"Ruleset '{name}': value mismatch at '{path}'\n"
                f"    local  = {json.dumps(local)}\n"
                f"    remote = {json.dumps(remote)}"
            )

    def _new_key(self, name: str, path: str) -> None:
        if self._ignored(path):
            return
        self.warnings.append(
            f"Ruleset '{name}': key '{path}' appeared in remote API response — "
            f"review and either track it or add to the ignore list"
        )

    def _removed_key(self, name: str, path: str) -> None:
        if self._ignored(path):
            return
        self.warnings.append(
            f"Ruleset '{name}': key '{path}' missing from remote API response — "
            f"review and either track it or add to the ignore list"
        )


def emit_annotations(diff: Diff) -> None:
    for w in diff.warnings:
        # Collapse to one line for the annotation form.
        print(f"::warning title=Ruleset drift (schema)::{w.splitlines()[0]}")
    for f in diff.failures:
        print(f"::error title=Ruleset drift (value)::{f.splitlines()[0]}")


def write_summary(diff: Diff, orphans: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = ["### Ruleset check", ""]

    if not diff.failures and not diff.warnings and not orphans:
        lines.append("✅ All resolved rulesets match the live rulesets.")
    else:
        if diff.failures:
            lines.append(f"#### ❌ Value mismatches (fail) — {len(diff.failures)}")
            lines.append("")
            for f in diff.failures:
                lines.append("```")
                lines.append(f)
                lines.append("```")
            lines.append("")
        if orphans:
            lines.append(f"#### ⚠️ Rulesets present on only one side — {len(orphans)}")
            lines.append("")
            for o in orphans:
                lines.append(f"- {o}")
            lines.append("")
        if diff.warnings:
            lines.append(f"#### ⚠️ Schema drift (warn) — {len(diff.warnings)}")
            lines.append("")
            for w in diff.warnings:
                lines.append(f"- {w.splitlines()[0]}")
            lines.append("")

    with open(summary_path, "a") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "usage: diff_rulesets.py <local_dir> <remote_dir> [config_file]",
            file=sys.stderr,
        )
        return 2

    local_dir = Path(sys.argv[1])
    remote_dir = Path(sys.argv[2])
    config_file = Path(sys.argv[3]) if len(sys.argv) > 3 else None

    strip, ignore = load_config(config_file) if config_file else (set(), set())
    diff = Diff(ignore)

    local_files = {p.name: p for p in local_dir.glob("*.json")}
    remote_files = {p.name: p for p in remote_dir.glob("*.json")}

    orphans: list[str] = []
    for only_local in sorted(set(local_files) - set(remote_files)):
        msg = (
            f"Ruleset '{only_local}' is defined locally but has no matching live "
            f"ruleset on the remote"
        )
        orphans.append(msg)
        print(f"::warning title=Ruleset added/removed::{msg}")
    for only_remote in sorted(set(remote_files) - set(local_files)):
        msg = (
            f"Ruleset '{only_remote}' exists on the remote but is not defined "
            f"locally"
        )
        orphans.append(msg)
        print(f"::warning title=Ruleset added/removed::{msg}")

    for fname in sorted(set(local_files) & set(remote_files)):
        local = load_json(local_files[fname])
        remote = load_json(remote_files[fname])
        if local is None or remote is None:
            diff.failures.append(f"Ruleset '{fname}': could not parse one side")
            continue
        name = local.get("name", fname)
        # Strip volatile/identity fields from both sides before comparing.
        local = strip_fields(local, strip)
        remote = strip_fields(remote, strip)
        diff.walk(name, local, remote, "")

    emit_annotations(diff)
    write_summary(diff, orphans)

    print(
        f"\nSummary: {len(diff.failures)} mismatch(es), "
        f"{len(diff.warnings)} schema warning(s), {len(orphans)} orphan(s)."
    )
    return 1 if diff.failures else 0


if __name__ == "__main__":
    sys.exit(main())
