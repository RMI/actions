#!/usr/bin/env python3
"""Nightly check: is GitHub's ruleset schema fully covered by our templates?

The per-PR check (diff_rulesets.py) only compares keys our templates define, so
it stays quiet when GitHub adds a field. This job closes that gap: it compares
GitHub's published ``repository-ruleset`` schema against what our templates
cover and flags drift **within the scope we manage** — top-level ruleset
properties, and parameters of the rule types our templates use. GitHub supports
~30 rule types; the ones we don't template are features we've chosen not to
adopt, not drift, so they are ignored (otherwise the report would be dozens of
false gaps). See ``_in_scope``.

Coverage is compared as dotted tokens (scope — top level + rules):
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


def _in_scope(token: str, covered_rule_types: set) -> bool:
    """Is a declared-but-uncovered token worth flagging as a gap?

    In scope: a top-level ruleset property (``name``, a new field GitHub adds),
    or a parameter of a rule type we actually template
    (``rules.<type>.parameters.<param>`` where ``rules.<type>`` is covered).

    Out of scope: a bare ``rules.<type>`` (adopting a new rule type is a product
    choice, not drift), and parameters of rule types we don't use.
    """
    if "." not in token:
        return True  # top-level ruleset property
    parts = token.split(".")
    if len(parts) >= 4 and parts[0] == "rules" and parts[2] == "parameters":
        return f"rules.{parts[1]}" in covered_rule_types
    return False


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
        """The list of concrete rule schemas under repository-rule (a oneOf)."""
        items = self.resolve(items)
        for combiner in ("oneOf", "anyOf", "allOf"):
            if combiner in items:
                return [self.resolve(v) for v in items[combiner]]
        return [items]

    def merged_properties(self, schema, _depth: int = 0) -> dict:
        """All ``properties`` of a schema, composing any ``allOf`` members.

        GitHub's OpenAPI sometimes defines a rule variant (or its ``parameters``)
        as ``{allOf: [{$ref: base}, {properties: {...}}]}``, where the real
        properties live inside the allOf members rather than at the top level.
        Reading only top-level ``properties`` would miss them and make those
        tokens look absent (→ false ``stale``). Merge them instead.
        """
        schema = self.resolve(schema)
        if _depth > 20:
            return {}
        props = dict(schema.get("properties", {}))
        for member in schema.get("allOf", []):
            props.update(self.merged_properties(member, _depth + 1))
        return props


def schema_tokens(openapi: dict) -> set:
    schemas = openapi.get("components", {}).get("schemas", {})
    if "repository-ruleset" not in schemas:
        raise SystemExit(
            "::error::'repository-ruleset' not found in schema components — "
            "OpenAPI layout may have changed"
        )
    w = SchemaWalker(schemas)
    ruleset = w.resolve(schemas["repository-ruleset"])
    props = w.merged_properties(ruleset)

    tokens: set = set(props.keys())

    rule_types: set = set()
    rules_prop = w.resolve(props.get("rules", {}))
    items = rules_prop.get("items")
    if items:
        for variant in w.rule_variants(items):
            vprops = w.merged_properties(variant)
            type_schema = w.resolve(vprops.get("type", {}))
            types = list(type_schema.get("enum", []))
            if "const" in type_schema:
                types.append(type_schema["const"])
            param_names = list(w.merged_properties(vprops.get("parameters", {})).keys())
            for t in types:
                rule_types.add(t)
                tokens.add(f"rules.{t}")
                for p in param_names:
                    tokens.add(f"rules.{t}.parameters.{p}")

    # Extracting zero rule types means the OpenAPI shape isn't what this parser
    # expects. Fail loudly rather than returning a token set that would make
    # every tracked rule look "stale" (a misleading nightly failure).
    if not rule_types:
        raise SystemExit(
            "::error::schema_coverage extracted 0 rule types from the "
            "repository-ruleset schema — its OpenAPI shape has likely changed; "
            "update schema_tokens()."
        )
    print(f"schema: parsed {len(rule_types)} rule types, {len(tokens)} tokens.")
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

    # Scope gap-detection to what we actually manage: top-level ruleset
    # properties, and parameters of the rule types our templates use. GitHub
    # supports ~30 rule types; we deliberately use a handful, so a bare
    # `rules.<type>` we don't template (merge_queue, tag_name_pattern, ...) is a
    # feature we've chosen not to adopt — not drift — and reporting it would bury
    # the real signal under dozens of false gaps. A new *parameter* on a rule
    # type we DO use is worth surfacing (we may want to track it).
    covered_rule_types = {
        t for t in covered if t.startswith("rules.") and t.count(".") == 1
    }
    gaps = {
        t for t in (declared - covered - acknowledged)
        if _in_scope(t, covered_rule_types)
    }
    # Keys we track that the schema no longer declares (rename/removal).
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
