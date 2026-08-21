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
│ │ └── gitflow-next-pr.json
│ ├── schema/
│ │ ├── overlay.schema.json
│ │ └── tracked-keys.json
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
      - uses: RMI/actions/actions/admin/rulesets-check@v1
        with:
          rulesets_dir: .github/rulesets   # default value if omitted
```

- `rulesets_dir` input defaults to `.github/rulesets` inside the action's own `action.yml`,
  so most repos can omit `with:` entirely.
- No nested/object inputs — `with:` only carries scalar strings, per GitHub Actions
  input constraints. All structure lives in the overlay files on the consumer's side.

### 3.2 Consumer repo's overlay files (option A: one file per ruleset)

Each ruleset the repo wants checked is represented by **one overlay file** in
`rulesets_dir`, name-matched to the template it extends:

```
.github/rulesets/
├── gitflow-main.overlay.json
├── gitflow-production.overlay.json
├── gitflow-next-lifecycle.overlay.json
├── gitflow-next-pr.overlay.json
└── copilot-review.overlay.json      # repo-specific, see "literal" note below
```

The filename stem before `.overlay.json` selects the template: `gitflow-main.overlay.json`
→ template `gitflow-main`. This mirrors the one-file-per-ruleset layout the repos already
have, so `git blame` on a small overlay reads cleanly and repo intent stays visible in
history rather than living in invisible action defaults.

An overlay is a **sparse** object holding only what this repo changes from the template.
The common case is a few fields deep inside one rule — e.g. tpr's entire `gitflow-main`
divergence:

```json
{
  "rules": {
    "pull_request": {
      "parameters": {
        "dismiss_stale_reviews_on_push": true,
        "require_last_push_approval": true
      }
    }
  }
}
```

An empty overlay (`{}`) means "take the template verbatim" — the explicit-file-per-ruleset
convention keeps that intent visible instead of implicit.

**Selecting the template explicitly / literal rulesets.** By default the template is
inferred from the filename stem. A repo-specific ruleset that maps to no bundled template
(e.g. tpr's `code-quality-copilot-review`) sets `"template": null` and supplies the whole
ruleset under `ruleset`:

```json
{ "template": null, "ruleset": { "...": "full literal ruleset object" } }
```

To point a differently-named file at a specific template, set `"template": "<name>"`
explicitly. There is only **one** mechanism here — template + overlay; a "literal" ruleset
is just the degenerate case of `template: null` where the overlay carries the entire body.

## 4. Bundled templates

Location: `actions/admin/rulesets-check/templates/*.json`, one file per named template,
keyed by filename (`gitflow-main.json` → template name `gitflow-main`). The current shared
set across `stitch` / `tpr` / `stitch-etl-poc` is **four** rulesets:

- `gitflow-main`
- `gitflow-production`
- `gitflow-next-lifecycle`
- `gitflow-next-pr`

(Repo-specific rulesets like tpr's `code-quality-copilot-review` are *not* templated —
they ride as `template: null` literals per §3.2.)

Templates are plain, **canonical** ruleset JSON objects — `rules` stored as a **list**,
exactly as GitHub's API returns them — minus the volatile fields already stripped today
(`id`, `current_user_can_bypass`, `_links`, `node_id`, `created_at`, `updated_at`,
`bypass_actors`). Reconcile the small per-repo divergences (see §12) into one canonical
default per template; anything a repo genuinely needs different lives in its overlay.

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

3. **Scalar arrays** (e.g. `conditions.ref_name.include`) → **replaced wholesale**: if the
   overlay provides the array, its value is used verbatim; otherwise the template's is
   kept. No element-wise union/subtract in v1 — predictable and unambiguous. Covers
   stitch's extra `refs/heads/demo/**` branch, which is expressed by restating that short
   list in the overlay.

Deferred (additive later, non-breaking): element-wise add/remove sugar for scalar arrays
(e.g. `{"include": {"add": ["refs/heads/demo/**"]}}`) if restating short lists becomes
annoying. Not needed for v1.

**Order-independence bonus.** Because both resolved-local and remote `rules` are keyed by
`type` before comparison (§6.3), the diff is insensitive to the arbitrary order GitHub's
API returns rules in — unlike the current `diff --recursive`, which false-positives on a
pure reorder.

## 6. Fetch / resolve / diff pipeline

Steps inside `actions/admin/rulesets-check/action.yml` (composite, no `concurrency:` —
each consumer's own caller workflow may add `concurrency:` at the job level if desired,
see §8):

1. **Fetch remote** (`scripts/fetch_remote.sh`, bash + `gh` + `jq` — unchanged from
   current logic): list ruleset IDs via `gh api /repos/{repo}/rulesets`, fetch each,
   strip volatile fields, write to `/tmp/remote_rulesets/<sanitized-name>.json`.

2. **Resolve local** (`scripts/resolve_local.py`, new): glob `<rulesets_dir>/*.overlay.json`
   (path from the `rulesets_dir` input). For each overlay file:
   - determine the template: `"template"` field if present (may be `null` for a literal),
     else the filename stem before `.overlay.json`,
   - load `templates/<template>.json` from `github.action_path` (skip when `template: null`;
     the overlay's `ruleset` body is the whole thing),
   - deep-merge the overlay onto the template per §5 (objects recurse, `rules` keyed by
     `type`, scalar arrays replaced), validating the one-rule-per-`type` invariant,
   - write each resolved ruleset (canonical, `rules` as a list) to
     `/tmp/local_rulesets/<sanitized-name>.json` — same directory shape the diff step
     expects, so downstream logic is identical whether a ruleset came from a template or a
     literal overlay.

3. **Diff** (`scripts/diff_rulesets.py`, new, replaces the current `diff --recursive`
   step): for each matching pair of remote/local JSON files, walk both structures
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

4. **Known-key ignore list** (`schema/tracked-keys.json`): optional companion file listing
   **dotted key paths** the team has explicitly decided to *always* ignore even if new
   (e.g. a GitHub-added field nobody cares about yet). Consulted by `diff_rulesets.py`
   before classifying a path as "new"/"removed" — if it matches the ignore list, skip
   silently rather than warning every run. Start empty; add entries as warnings get
   triaged. `bypass_actors` (see §4) is stripped upstream and so is implicitly ignored;
   if we ever decide to track it, this is where the decision surfaces.

## 7. Reusable-workflow wrapper (optional, decide later)

If the team decides it wants the "zero caller boilerplate" ergonomic (one line under
`jobs:` instead of a full job with checkout), add a thin `.github/workflows/admin/check-
rulesets.yml` in `RMI/actions` that itself calls the composite action:

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
      - uses: RMI/actions/actions/admin/rulesets-check@v1
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

- Tag `RMI/actions` releases (`v1`, moving forward like `actions/checkout@v4`) rather
  than having consumers point at `@main`.
  - Consumers use `@v1` for convenience; anything security-sensitive can pin `@<sha>`.
- Template changes (new/updated `templates/*.json`) ship as normal commits/releases in
  `RMI/actions`, same cadence as action-logic changes.

## 10. GitHub API schema reference

`github/rest-api-description` (public repo) publishes GitHub's OpenAPI 3.0/3.1
description, including the `repository-ruleset` schema component, and is the same source
GitHub uses to generate Octokit and its own API docs — treat as authoritative and
current.

Optional follow-up (not required for v1, good future addition): a separate, non-blocking
**scheduled** job in `RMI/actions` that pulls the current `repository-ruleset` schema from
that repo and diffs its property list against `schema/tracked-keys.json`, opening an issue
against `RMI/actions` when the upstream schema gains/loses a property — catching drift
proactively instead of reactively on a consumer repo's PR.

## 11. Migration steps for existing consumer repos

1. Author `RMI/actions/actions/admin/rulesets-check/` per §2–§6, tag `v1`.
2. For each of `stitch`, `stitch-etl-poc`, `tpr`:
   - Diff their existing `.github/rulesets/*.json` against the new bundled templates —
     confirm which map cleanly to `gitflow-{main,production,next-lifecycle,next-pr}` vs.
     need to stay literal (`template: null`, e.g. tpr's `code-quality-copilot-review`).
   - Write one `<name>.overlay.json` per ruleset holding only that repo's deltas (most
     will be `{}` or a few fields; tpr's `gitflow-main` gets the `pull_request` params from
     §3.2, and its duplicate `copilot_code_review` rule is fixed at the same time).
   - Replace `.github/workflows/admin-check_rulesets.yml` contents with the thin caller
     from §3.1 (or §7's reusable-workflow form, if built).
   - Delete the old hand-authored ruleset JSON files once overlays cover them.
   - Confirm a run passes (or produces only expected warnings) before merging.

## 12. Open decisions to confirm during implementation

- Exact canonical field values for each of the **four** bundled templates
  (`gitflow-main`, `gitflow-production`, `gitflow-next-lifecycle`, `gitflow-next-pr`) —
  pull from current repos' existing JSON and reconcile the known divergences:
  - `gitflow-main` `pull_request` params differ (tpr: `dismiss_stale_reviews_on_push` +
    `require_last_push_approval` both `true`; stitch/etl both `false`) — pick the default,
    the odd repo carries an overlay.
  - `gitflow-next-lifecycle` `conditions.ref_name.include` differs (stitch adds
    `refs/heads/demo/**`) — decide whether `demo/**` is a default or a stitch overlay.
  - tpr's `gitflow-main` duplicate `copilot_code_review` rule is drift — drop it.
- Initial contents (if any) of `schema/tracked-keys.json` ignore list.
- Whether `bypass_actors` should stay stripped/untracked (current behavior) or be brought
  into scope — it's security-relevant (who can skip the rules) but noisy.
- Whether to build the §7 reusable-workflow wrapper now (recommended — see §7 note) or defer.
- Whether `resolve_local.py` / `diff_rulesets.py` should validate each overlay against
  `schema/overlay.schema.json` up front (recommended: yes, fail fast with a clear error
  rather than a confusing downstream diff).
