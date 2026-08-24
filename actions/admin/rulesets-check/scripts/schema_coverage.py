#!/usr/bin/env python3
"""Nightly check: is GitHub's ruleset schema fully covered by our templates?

The per-PR check (diff_rulesets.py) only compares keys our templates define, so
it stays quiet when GitHub adds a field. This job closes that gap: it compares
GitHub's published ``repository-ruleset`` schema against what our templates
cover and reports any schema property we neither track nor have explicitly
acknowledged as out-of-scope.

Coverage is compared as dotted tokens (PLAN.md §10 scope — top level + rules):
  * ``<prop>``                          — top-level ruleset property
  * ``rules.<type>``                    — a rule variant
  * ``rules.<type>.parameters.<param>`` — a rule parameter

`conditions` internals are treated as a single top-level token on both sides
(out of scope for v1; documented).

Usage:
  schema_coverage.py <templates_dir> <acknowledged_file> <openapi_json>

Exits **non-zero on drift in either direction** — a schema property we don't
cover (`gaps`), or a key we track that the schema no longer lists (`stale`, a
rename/removal) — so the nightly scheduled run fails. A failed run is the signal
(no issue is opened). The human report is written to stdout and
$GITHUB_STEP_SUMMARY.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def load_json(p: Path):
    return json.loads(Path(p).read_text())


# --- what our templates cover -------------------------------------------------

def template_tokens(templates_dir: Path) -> set:
    tokens: set = set()
    for f in sorted(templates_dir.glob("*.json")):
        data = load_json(f)
        if not isinstance(data, dict):
            continue
        for key in data:
            tokens.add(key)  # top-level ruleset property
        for rule in data.get("rules", []):
            if not isinstance(rule, dict) or "type" not in rule:
                continue
            rtype = rule["type"]
            tokens.add(f"rules.{rtype}")
            for pname in (rule.get("parameters") or {}):
                tokens.add(f"rules.{rtype}.parameters.{pname}")
    return tokens


# --- what GitHub's schema declares -------------------------------------------

class SchemaWalker:
    def __init__(self, schemas: dict):
        self.schemas = schemas

    def resolve(self, node, _depth: int = 0):
        """Follow local $ref chains to a concrete schema object."""
        if not isinstance(node, dict) or _depth > 20:
            return node if isinstance(node, dict) else {}
        ref = node.get("$ref")
        if ref:
            name = ref.split("/")[-1]
            target = self.schemas.get(name)
            if target is None:
                return {}
            return self.resolve(target, _depth + 1)
        return node

    def rule_variants(self, items: dict) -> list:
        """The list of concrete rule schemas under repository-rule."""
        items = self.resolve(items)
        for combiner in ("oneOf", "anyOf", "allOf"):
            if combiner in items:
                return [self.resolve(v) for v in items[combiner]]
        return [items]


def schema_tokens(openapi: dict) -> set:
    schemas = openapi.get("components", {}).get("schemas", {})
    if "repository-ruleset" not in schemas:
        raise SystemExit(
            "::error::'repository-ruleset' not found in schema components — "
            "OpenAPI layout may have changed"
        )
    w = SchemaWalker(schemas)
    ruleset = w.resolve(schemas["repository-ruleset"])
    props = ruleset.get("properties", {})

    tokens: set = set(props.keys())

    rules_prop = w.resolve(props.get("rules", {}))
    items = rules_prop.get("items")
    if items:
        for variant in w.rule_variants(items):
            vprops = variant.get("properties", {})
            type_schema = vprops.get("type", {})
            types = list(type_schema.get("enum", []))
            if "const" in type_schema:
                types.append(type_schema["const"])
            params = w.resolve(vprops.get("parameters", {}))
            param_names = list(params.get("properties", {}).keys())
            for t in types:
                tokens.add(f"rules.{t}")
                for p in param_names:
                    tokens.add(f"rules.{t}.parameters.{p}")
    return tokens


# --- report -------------------------------------------------------------------

def render(gaps: set, stale: set) -> str:
    lines = ["## Ruleset schema coverage", ""]
    if not gaps and not stale:
        lines.append("✅ Templates cover every property in GitHub's current ruleset schema.")
        return "\n".join(lines) + "\n"

    if gaps:
        lines.append(f"### ⚠️ Uncovered schema properties — {len(gaps)}")
        lines.append("")
        lines.append("GitHub's ruleset schema has these we neither track nor acknowledged:")
        lines.append("")
        for g in sorted(gaps):
            lines.append(f"- `{g}`")
        lines.append("")
        lines.append(
            "Decide for each: **track it** (add to a template in "
            "`actions/admin/rulesets-check/templates/`) or **acknowledge** it "
            "(add to `schema/acknowledged-untracked.json`)."
        )
        lines.append("")
    if stale:
        lines.append(f"### ℹ️ Tracked keys absent from the schema — {len(stale)}")
        lines.append("")
        lines.append("We track these but GitHub's schema no longer lists them (renamed/removed?):")
        lines.append("")
        for s in sorted(stale):
            lines.append(f"- `{s}`")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "usage: schema_coverage.py <templates_dir> <acknowledged_file> <openapi_json>",
            file=sys.stderr,
        )
        return 2

    templates_dir = Path(sys.argv[1])
    acknowledged_file = Path(sys.argv[2])
    openapi_path = Path(sys.argv[3])

    covered = template_tokens(templates_dir)

    ack_data = load_json(acknowledged_file) if acknowledged_file.is_file() else {}
    acknowledged = set(ack_data.get("keys", []) if isinstance(ack_data, dict) else [])

    declared = schema_tokens(load_json(openapi_path))

    gaps = declared - covered - acknowledged
    # Keys we track that the schema doesn't declare (informational only).
    stale = covered - declared - acknowledged

    report = render(gaps, stale)
    print(report)

    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            fh.write(report)

    print(
        f"coverage: {len(covered)} tracked, {len(declared)} declared, "
        f"{len(gaps)} uncovered, {len(stale)} stale."
    )
    # Fail the (nightly) run on either drift direction, so a failed scheduled run
    # is the alert:
    #   * gaps  — the schema has a property we neither track nor acknowledged.
    #   * stale — we track a key the schema no longer lists (a GitHub rename/
    #     removal), which will start failing every consumer PR until the template
    #     is updated.
    return 1 if (gaps or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
