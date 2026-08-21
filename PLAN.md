# Plan: Centralize Ruleset Checks in RMI/actions

## 1. Goal

Move the ruleset-drift check currently duplicated in `RMI/stitch`, `RMI/stitch-etl-poc`,
and `RMI/tpr` (`.github/workflows/admin-check_rulesets.yml`) into a single, centrally
maintained composite action in `RMI/actions`, with:

- One place to fix logic when GitHub's rulesets API changes.
- Bundled default ruleset templates that consumer repos inherit and adjust with a small
  **overlay** file, instead of hand-authoring full ruleset JSON in every repo.
- A warn-vs-fail distinction: new/removed keys (schema drift) should warn, not fail;
  actual value mismatches on tracked keys should fail.

## 2. Repo layout (`RMI/actions`)

```
RMI/actions/
├── actions/
│ └── admin/
│ ├── forbidden-patterns/ # existing
│ │ └── action.yml
│ └── rulesets-check/ # new
│ ├── action.yml
│ ├── scripts/
│ │ ├── fetch_remote.sh
│ │ ├── resolve_local.py # template + overlay → resolved ruleset
│ │ └── diff_rulesets.py
│ ├── templates/ # one file per shared ruleset (canonical, list-form rules)
│ │ ├── gitflow-main.json
│ │ ├── gitflow-production.json
│ │ ├── gitflow-next-lifecycle.json
│ │ ├── gitflow-next-pr.json
│ │ └── blank.json # minimal base for fully custom rulesets
│ ├── schema/
│ │ ├── overlay.schema.json # reference schema for editor tooling
│ │ └── diff-config.json # { strip: [...], ignore: [...] } — drives the diff
│ └── README.md
├── .github/
│ └── workflows/
│ └── admin/
│ └── check-rulesets.yml # thin reusable-workflow wrapper (optional, see §7)
├── README.md
└── LICENSE.txt
```

Rationale: composite action code (`actions/admin/rulesets-check/`) is self-contained and
versions with `github.action_path`, so templates and scripts ship together with the action
itself — no separate fetch needed. The reusable-workflow wrapper is optional; see §7 for
why you might still want one.

## 3. Consumer-repo interface

### 3.1 Caller (composite-action form, in each consumer repo's own workflow)

```yaml
name: Rulesets Checks
on:
  push:
    branches: [main]
  pull_request:

jobs:
  rulesets:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: RMI/actions/actions/admin/rulesets-check@main
        with:
          rulesets_dir: .github/rulesets   # default value if omitted
```

- `rulesets_dir` input defaults to `.github/rulesets` inside the action's own `action.yml`,
  so most repos can omit `with:` entirely.
- No nested/object inputs — `with:` only carries scalar strings, per GitHub Actions
  input constraints. All structure lives in the overlay files on the consumer's side.

### 3.2 Consumer repo's overlay files (option A: one file per ruleset)

Each ruleset the repo wants checked is represented by **one overlay file** in
`rulesets_dir`:

```
.github/rulesets/
├── gitflow-main.overlay.json
├── gitflow-production.overlay.json
├── gitflow-next-lifecycle.overlay.json
├── gitflow-next-pr.overlay.json
└── copilot-review.overlay.json      # fully custom, uses the "blank" template
```

Every overlay **must** name its `template` explicitly (no filename inference — the filename
is free to be descriptive). This keeps `git blame` on a small overlay readable and repo
intent visible in history rather than living in invisible action defaults.

An overlay is a **sparse** object naming a template plus only what this repo changes from
it. The common case is a few fields deep inside one rule — e.g. a repo overriding one
`pull_request` parameter on top of `gitflow-production`:

```json
{
  "template": "gitflow-production",
  "rules": {
    "pull_request": {
      "parameters": { "required_approving_review_count": 1 }
    }
  }
}
```

A template-only overlay (`{ "template": "gitflow-main" }`) means "take the template
verbatim".

**Fully custom rulesets.** A repo-specific ruleset that maps to no gitflow template
(e.g. tpr's `code-quality-copilot-review`) uses the bundled **`blank`** template (a minimal
base: `target: branch`, `enforcement: active`, no rules) and authors the whole ruleset in
the overlay, in the *same* overlay format (rules as a `{type: patch}` map):

```json
{
  "template": "blank",
  "name": "code-quality-copilot-review",
  "conditions": { "ref_name": { "include": ["~ALL"], "exclude": [] } },
  "rules": { "copilot_code_review": {} }
}
```

There is exactly **one** mechanism — template + overlay. Requiring `template` and shipping
`blank` removes both the filename-inference magic and the separate `template: null` /
`ruleset` literal path (an earlier design), so every entry flows through the identical
merge + validation code path.

## 4. Bundled templates

Location: `actions/admin/rulesets-check/templates/*.json`, one file per named template,
keyed by filename (`gitflow-main.json` → template name `gitflow-main`). The current shared
set across `stitch` / `tpr` / `stitch-etl-poc` is **four** rulesets, plus a `blank` base:

- `gitflow-main`
- `gitflow-production`
- `gitflow-next-lifecycle`
- `gitflow-next-pr`
- `blank` — minimal base (`target: branch`, `enforcement: active`, no rules) for fully
  custom rulesets (e.g. tpr's `code-quality-copilot-review`), per §3.2.

Templates are plain, **canonical** ruleset JSON objects — `rules` stored as a **list**,
exactly as GitHub's API returns them — minus the volatile / identity fields listed in
`schema/diff-config.json` (`id`, `node_id`, `_links`, `created_at`, `updated_at`,
`current_user_can_bypass`, `bypass_actors`, `source`, `source_type`). `source` /
`source_type` are per-repo identity (`"source": "RMI/<repo>"`) with no drift signal; the
same `strip` list is applied to the remote side at diff time (§6), so shared templates
don't false-positive on repo name.

`required_status_checks` contexts are **100% per-repo** (each repo's CI job names differ),
so templates ship an **empty** contexts list and every repo supplies its own via overlay
(wholesale array replace, §5.3). The check still catches a required check being deleted
from the live ruleset.

Reconcile the small per-repo divergences (see §12) into one canonical default per template;
anything a repo genuinely needs different lives in its overlay.

Templates version with the action itself (same tag/SHA), so bumping `RMI/actions` to a
new template revision is a normal PR + release cycle there, and consumers pick it up on
their next run against the pinned ref.

## 5. Merge algorithm (template + overlay → resolved local ruleset)

**v1 scope: recursive (deep) merge, with an explicit policy for the two array shapes that
actually occur in these rulesets.** This replaces the earlier shallow-merge plan, which
could not reach the fields where the repos actually differ — every real divergence lives
*inside* `rules[]` or `conditions`, not at the top level.

Merge rules, applied by `resolve_local.py`:

1. **Objects** → recurse; a leaf scalar present in the overlay wins over the template.
   Handles the common case (e.g. `pull_request.parameters.dismiss_stale_reviews_on_push`).

2. **`rules[]` → address by `.type`.** Rules are the one array where element identity
   matters, and `type` *is* that identity. The resolver:
   - indexes the template's `rules` list into a `{type: rule}` map,
   - reads the overlay's `rules` as a `{type: patch}` **map** (note: overlay expresses
     rules as a type-keyed object, not a list — this is what makes overlays sparse and
     readable; the resolver converts back to a list on output),
   - deep-merges each patch onto the matching template rule (recursing per rule 1),
   - a type present only in the overlay is **added**; a type set to `null` in the overlay
     is **removed**,
   - re-emits `rules` as a canonical **list** for diffing.
   - **Invariant: at most one rule per `type` per ruleset.** Both template and resolved
     output are validated for this; a duplicate `type` is a hard error. (This is exactly
     the drift found in tpr's `gitflow-main`, which carries two `copilot_code_review`
     rules — the check will now surface it instead of silently picking one.)

3. **All other arrays** (`conditions.ref_name.include`, and
   `required_status_checks.parameters.required_status_checks` — an array of
   `{context, integration_id}` objects) → **replaced wholesale**: if the overlay provides
   the array, its value is used verbatim; otherwise the template's is kept. No element-wise
   union/subtract or index-merge in v1 — predictable and unambiguous. This covers both
   stitch's extra `refs/heads/demo/**` branch and the per-repo required-status-check
   contexts (see §4: templates ship empty contexts, each repo supplies its own list here).

Deferred (additive later, non-breaking): element-wise add/remove sugar for arrays
(e.g. `{"include": {"add": ["refs/heads/demo/**"]}}`) if restating lists becomes annoying.
Not needed for v1.

**Order-independence bonus.** Because both resolved-local and remote `rules` are keyed by
`type` before comparison (§6.3), the diff is insensitive to the arbitrary order GitHub's
API returns rules in — unlike the current `diff --recursive`, which false-positives on a
pure reorder.

## 6. Fetch / resolve / diff pipeline

Steps inside `actions/admin/rulesets-check/action.yml` (composite, no `concurrency:` —
each consumer's own caller workflow may add `concurrency:` at the job level if desired,
see §8):

1. **Fetch remote** (`scripts/fetch_remote.sh`, bash + `gh` + `jq`): list ruleset IDs via
   `gh api /repos/{repo}/rulesets`, fetch each, sanitize name, write the **raw** API
   response to `/tmp/remote_rulesets/<sanitized-name>.json`. No field stripping here —
   stripping is centralized in the diff step (§6.3) from `schema/diff-config.json`, so
   there's a single source of truth and the bash stays trivial.

2. **Resolve local** (`scripts/resolve_local.py`, new): glob `<rulesets_dir>/*.overlay.json`
   (path from the `rulesets_dir` input). For each overlay file:
   - read the **required** `"template"` field (no filename inference); error if missing or
     unknown. Use `blank` for fully custom rulesets.
   - load `templates/<template>.json` from `github.action_path`,
   - deep-merge the overlay onto the template per §5 (objects recurse, `rules` keyed by
     `type`, other arrays replaced), validating the one-rule-per-`type` invariant,
   - write each resolved ruleset (canonical, `rules` as a list) to
     `/tmp/local_rulesets/<sanitized-name>.json` — the shared directory shape the diff step
     expects, so every entry (templated or custom) flows through identical downstream logic.

3. **Diff** (`scripts/diff_rulesets.py`, new, replaces the current `diff --recursive`
   step): first **strip** the `strip` fields (from `schema/diff-config.json`) off **both**
   sides, then, for each matching pair of remote/local JSON files, walk both structures
   **recursively** and classify at every level — the warn-vs-fail distinction has to be
   recursive because GitHub adds/removes fields *inside* `rules[].parameters` and
   `conditions`, not just at the top level. At each node:
   - **Normalize `rules` to a `{type: rule}` map** on both sides before comparing, so the
     walk is order-independent and keys line up by rule identity rather than list index.
   - `tracked = local ∩ remote` keys: recurse into objects; at leaves compare values, any
     mismatch → **fail** (collect all, `exit 1` at end of script after processing every
     ruleset, so one bad ruleset doesn't hide others).
   - `new = remote − local` (GitHub added a field, at any depth) and
     `removed = local − remote` (GitHub removed/renamed, or local references a key gone
     from remote): emit
     `::warning::Ruleset '<name>': key '<dotted.path>' <appeared in|missing from> remote
     API response — review and either track it or add to the ignore list`, append detail
     to `$GITHUB_STEP_SUMMARY`, and **do not fail** on these alone.
   - Path is reported dotted (e.g. `rules.pull_request.parameters.require_last_push_approval`)
     so a warning points at the exact drift site.
   - Rulesets present remotely but missing from local (or vice versa) — no matching pair at
     all — are a separate warning class (`ruleset added/removed entirely`), not a per-key
     diff.

4. **Diff config** (`schema/diff-config.json`): single companion file with two lists, both
   consulted by `diff_rulesets.py`:
   - **`strip`** — top-level fields removed from **both** sides before comparing (volatile,
     server-assigned, or per-repo identity — never a drift signal). See §4 for the list.
   - **`ignore`** — **dotted key paths** to *always* skip when classifying new/removed keys
     (e.g. a GitHub-added field nobody cares about yet). Start empty; add entries as
     warnings get triaged.
   `bypass_actors` (see §4) lives in `strip` and so is invisible to the diff; if we ever
   decide to track it, removing it from `strip` is the one-line switch.

## 7. Reusable-workflow wrapper (built)

For the "zero caller boilerplate" ergonomic (one line under `jobs:` instead of a full job
with checkout), `.github/workflows/admin/check-rulesets.yml` in `RMI/actions` wraps the
composite action:

```yaml
on:
  workflow_call:
    inputs:
      rulesets_dir:
        type: string
        default: .github/rulesets
jobs:
  rulesets:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: RMI/actions/actions/admin/rulesets-check@main
        with:
          rulesets_dir: ${{ inputs.rulesets_dir }}
```

Note: all three consumer repos today already invoke via `workflow_call` (their
`admin-check_rulesets.yml` is a reusable workflow), so shipping this wrapper makes
migration close to a drop-in — worth building alongside v1 rather than deferring.

This is additive and doesn't preclude direct composite-action use — some repos may call
the action directly, others via the workflow wrapper. Not required for v1; add if the
three-line consumer boilerplate becomes annoying in practice.

## 8. Concurrency

Not built into the action (composite actions can't declare `concurrency:` — it's a
job/workflow-level key). If a consumer repo wants it, add to their own caller job:

```yaml
jobs:
  rulesets:
    concurrency: rulesets-checks
    runs-on: ubuntu-latest
    steps: [...]
```

Given the check is read-only (API reads + local diff, no mutation), skipping this
entirely is low-risk — worst case is two overlapping runs both harmlessly reporting the
same diff.

## 9. Versioning

- **For now, consumers track `@main`.** There is a single known user (the author), so the
  overhead of a tag-and-release cadence isn't worth it yet — `@main` keeps everyone on the
  latest logic and templates automatically.
- Revisit tagged releases (`v1`, moving forward like `actions/checkout@v4`) if/when there
  are more consumers who need a stable pin; anything security-sensitive can already pin
  `@<sha>` today.
- Template changes (new/updated `templates/*.json`) ship as normal commits to `main`, same
  cadence as action-logic changes.

## 10. GitHub API schema reference

`github/rest-api-description` (public repo) publishes GitHub's OpenAPI 3.0/3.1
description, including the `repository-ruleset` schema component, and is the same source
GitHub uses to generate Octokit and its own API docs — treat as authoritative and
current.

Optional follow-up (not required for v1, good future addition): a separate, non-blocking
**scheduled** job in `RMI/actions` that pulls the current `repository-ruleset` schema from
that repo and diffs its property list against `schema/diff-config.json`, opening an issue
against `RMI/actions` when the upstream schema gains/loses a property — catching drift
proactively instead of reactively on a consumer repo's PR.

## 11. Migration steps for existing consumer repos

1. Author `RMI/actions/actions/admin/rulesets-check/` per §2–§6 on `main`.
2. For each of `stitch`, `stitch-etl-poc`, `tpr`:
   - Diff their existing `.github/rulesets/*.json` against the new bundled templates —
     confirm which map cleanly to `gitflow-{main,production,next-lifecycle,next-pr}` vs.
     need the `blank` template (e.g. tpr's `code-quality-copilot-review`).
   - Write one `<name>.overlay.json` per ruleset — each names its `template` and holds only
     that repo's deltas (most are template-only or a few fields; tpr's `gitflow-main` gets
     the `pull_request` params from §3.2, and its duplicate `copilot_code_review` rule is
     fixed at the same time).
   - Replace `.github/workflows/admin-check_rulesets.yml` contents with the thin caller
     from §3.1 (or §7's reusable-workflow form, if built).
   - Delete the old hand-authored ruleset JSON files once overlays cover them.
   - Confirm a run passes (or produces only expected warnings) before merging.

## 12. Decisions (resolved) & remaining follow-ups

### 12.1 Canonical template values (resolved)

Base = **stitch**, with these deliberate adjustments (the standard the repos converge to):

| Template | Field | Canonical default | Notes |
|---|---|---|---|
| `gitflow-main` | `pull_request.dismiss_stale_reviews_on_push` | `true` | from tpr; stitch/etl live = `false` (converge) |
| `gitflow-main` | `pull_request.require_last_push_approval` | `true` | from tpr; stitch/etl live = `false` (converge) |
| `gitflow-main` | one rule per `type` | enforced | drops tpr's duplicate `copilot_code_review` |
| `gitflow-production` | `pull_request.required_approving_review_count` | `2` | stitch value; **tpr overrides to 1** via overlay |
| `gitflow-next-lifecycle` | `conditions.ref_name.include` | includes `refs/heads/demo/**` | from stitch; tpr/etl live lack it (converge) |
| all (main/prod) | `required_status_checks[].context` list | **empty** | per-repo; each repo overlays its own (§4) |

Everything else follows stitch verbatim. `gitflow-next-pr` is identical across all three
repos — pure stitch. `source` / `source_type` omitted from templates and stripped remote-side.

### 12.2 Convergence action items (chosen: update live rulesets, not pin overlays)

Because templates are the source of truth and reality must catch up, once this lands the
**live GitHub rulesets** for these repos need updating (until then their checks report
drift, by design):

- **stitch, etl**: flip `gitflow-main` `dismiss_stale_reviews_on_push` and
  `require_last_push_approval` to `true`.
- **tpr, etl**: add `refs/heads/demo/**` to `gitflow-next-lifecycle` includes.
- **tpr**: remove the duplicate `copilot_code_review` rule from `gitflow-main`; keep its
  `gitflow-production` review count at 1 via **overlay** (this one is a permanent per-repo
  policy, not a convergence gap).

### 12.3 Remaining follow-ups (non-blocking)

- Initial `schema/diff-config.json` `ignore` list: start **empty**; `strip` holds the
  volatile/identity fields (§4).
- `bypass_actors` stays in `strip` (untracked) for now (security-relevant but noisy; revisit).
- §7 reusable-workflow wrapper: **built** (all three repos already use `workflow_call`).
- Overlay validation: enforced **inline** in `resolve_local.py` (missing/unknown `template`,
  missing `name`, duplicate rule types, invalid JSON) — fails fast with a clear error and
  avoids a runtime `jsonschema` dependency. `schema/overlay.schema.json` ships for editor
  tooling.
