#!/usr/bin/env python3
"""Resolve a repo's ruleset overlays against bundled templates.

For each ``<name>.overlay.json`` in the rulesets dir, load its template
(bundled with the action), deep-merge the overlay on top per the merge rules
below (also documented in the action README), and write the resolved, canonical ruleset (rules as a list) to the
output dir. Downstream, diff_rulesets.py compares these against the live remote
rulesets.

Merge semantics (§5):
  * objects        -> recurse; overlay leaf scalars win.
  * ``rules``      -> addressed by ``.type``. Template stores rules as a list;
                     the overlay expresses them as a ``{type: patch}`` map. Each
                     patch is deep-merged onto the matching template rule. A type
                     only in the overlay is added; a type mapped to ``null`` is
                     removed. Output is re-emitted as a canonical list.
                     Invariant: at most one rule per ``type``.
  * other arrays   -> replaced wholesale (no element-wise merge).

Overlay file shape:
  {
    "template": "gitflow-main",   # REQUIRED — a bundled template name. Use the
                                  # "blank" template to author a fully custom
                                  # ruleset entirely from the overlay.
    <ruleset fields to override, with `rules` as a {type: patch} map> ...
  }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OVERLAY_SUFFIX = ".overlay.json"
# Keys with meaning to the resolver rather than being ruleset fields themselves.
CONTROL_KEYS = {"template"}


def die(msg: str) -> "None":
    print(f"::error title=rulesets-check (resolve)::{msg}", file=sys.stderr)
    sys.exit(1)


def rules_list_to_map(rules: list, source: str) -> dict:
    """Index a canonical ``rules`` list by ``.type``, enforcing uniqueness."""
    out: dict = {}
    for rule in rules:
        if not isinstance(rule, dict) or "type" not in rule:
            die(f"{source}: every entry in 'rules' must be an object with a 'type'")
        rtype = rule["type"]
        if rtype in out:
            die(
                f"{source}: duplicate rule type '{rtype}' — a ruleset may hold at "
                f"most one rule per type"
            )
        # Drop the redundant 'type' key inside the value; it's the map key.
        out[rtype] = {k: v for k, v in rule.items() if k != "type"}
    return out


def rules_map_to_list(rules_map: dict) -> list:
    """Re-emit a ``{type: rule-body}`` map as a canonical ``rules`` list."""
    out = []
    for rtype, body in rules_map.items():
        rule = {"type": rtype}
        rule.update(body)
        out.append(rule)
    return out


def deep_merge(base, overlay, path: str):
    """Recursively merge ``overlay`` onto ``base`` per §5.

    ``base`` comes from the template (canonical shapes); ``overlay`` is the
    sparse repo file. Neither input is mutated.
    """
    # Overlay value replaces base outright unless both sides are dicts.
    if not isinstance(overlay, dict) or not isinstance(base, dict):
        return overlay

    merged = dict(base)
    for key, ov_val in overlay.items():
        loc = f"{path}.{key}" if path else key

        if key == "rules":
            # Special case: rules are keyed by type on both sides for merging.
            base_rules = base.get("rules", [])
            if not isinstance(base_rules, list):
                die(f"{loc}: template 'rules' must be a list")
            base_map = rules_list_to_map(base_rules, f"template:{loc}")
            if not isinstance(ov_val, dict):
                die(
                    f"{loc}: overlay 'rules' must be an object keyed by rule type "
                    f"(e.g. {{\"pull_request\": {{...}}}}), not a list"
                )
            result_map = dict(base_map)
            for rtype, patch in ov_val.items():
                if patch is not None and not isinstance(patch, dict):
                    die(
                        f"{loc}.{rtype}: a rule patch must be an object (to merge/add) "
                        f"or null (to remove), not {type(patch).__name__}"
                    )
                if patch is None:
                    result_map.pop(rtype, None)  # explicit removal
                elif rtype in result_map:
                    result_map[rtype] = deep_merge(
                        result_map[rtype], patch, f"{loc}.{rtype}"
                    )
                else:
                    # New rule type: strip a redundant nested 'type' if present.
                    result_map[rtype] = {k: v for k, v in patch.items() if k != "type"}
            merged["rules"] = rules_map_to_list(result_map)
        elif isinstance(ov_val, dict) and isinstance(base.get(key), dict):
            merged[key] = deep_merge(base[key], ov_val, loc)
        else:
            # Scalars, and all non-`rules` arrays (e.g. status-check contexts,
            # ref_name.include) -> wholesale replace.
            merged[key] = ov_val
    return merged


def sanitize(name: str) -> str:
    """Match the remote-side name sanitization (tr -c 'a-zA-Z0-9._-' '_')."""
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_")
    return "".join(c if c in allowed else "_" for c in name)


def load_json(p: Path):
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        die(f"file not found: {p}")
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {p}: {e}")


def main() -> int:
    if len(sys.argv) != 4:
        die("usage: resolve_local.py <rulesets_dir> <templates_dir> <out_dir>")

    rulesets_dir = Path(sys.argv[1])
    templates_dir = Path(sys.argv[2])
    out_dir = Path(sys.argv[3])
    out_dir.mkdir(parents=True, exist_ok=True)

    if not rulesets_dir.is_dir():
        die(f"rulesets_dir does not exist: {rulesets_dir}")

    overlays = sorted(rulesets_dir.glob(f"*{OVERLAY_SUFFIX}"))
    if not overlays:
        print(
            f"::warning title=rulesets-check::no *{OVERLAY_SUFFIX} files found in "
            f"{rulesets_dir} — nothing to resolve"
        )
        return 0

    written = 0
    seen: dict = {}  # sanitized filename -> overlay that produced it
    for overlay_path in overlays:
        overlay = load_json(overlay_path)
        if not isinstance(overlay, dict):
            die(f"{overlay_path}: overlay must be a JSON object")

        # A template is always required and always names a bundled template.
        # For a fully custom ruleset, use the "blank" template and supply
        # everything from the overlay.
        template_name = overlay.get("template")
        if not template_name:
            die(
                f"{overlay_path}: missing required 'template' field. Name a bundled "
                f"template (use \"blank\" to author a fully custom ruleset)."
            )
        # `template` must be a bare bundled-template name, not a path. Reject
        # non-strings and anything with a path separator so a value like
        # "../../etc/passwd" or "/abs/path" can't escape templates_dir.
        if not isinstance(template_name, str) or "/" in template_name or "\\" in template_name:
            die(
                f"{overlay_path}: invalid template {template_name!r} — must be a bare "
                f"bundled template name (e.g. \"gitflow-main\"), not a path."
            )

        template_path = templates_dir / f"{template_name}.json"
        if not template_path.is_file():
            available = ", ".join(sorted(p.stem for p in templates_dir.glob("*.json")))
            die(
                f"{overlay_path}: unknown template '{template_name}'. "
                f"Available: {available}"
            )
        template = load_json(template_path)
        # Everything except control keys is merge material.
        merge_src = {k: v for k, v in overlay.items() if k not in CONTROL_KEYS}
        resolved = deep_merge(template, merge_src, "")
        # Post-merge invariant check (catches an overlay that added a dup type).
        rules_list_to_map(resolved.get("rules", []), f"{overlay_path}:resolved.rules")

        name = resolved.get("name")
        if not name:
            die(f"{overlay_path}: resolved ruleset has no 'name'")

        san = sanitize(name)
        if san in seen:
            die(
                f"{overlay_path}: resolved ruleset name '{name}' collides with the "
                f"one from {seen[san].name} — both map to '{san}.json'. Two rulesets "
                f"can't share a (sanitized) name; rename one."
            )
        seen[san] = overlay_path

        out_path = out_dir / f"{san}.json"
        out_path.write_text(json.dumps(resolved, indent=2, sort_keys=True) + "\n")
        print(f"resolved {overlay_path.name} (template={template_name}) -> {out_path.name}")
        written += 1

    print(f"resolved {written} ruleset(s) into {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
