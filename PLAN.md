# Plan: Centralize Ruleset Checks in RMI/actions

## 1. Goal

Move the ruleset-drift check currently duplicated in `RMI/stitch`, `RMI/stitch-etl-poc`,
and `RMI/tpr` (`.github/workflows/admin-check_rulesets.yml`) into a single, centrally
maintained composite action in `RMI/actions`, with:

- One place to fix logic when GitHub's rulesets API changes.
- Bundled default ruleset templates that consumer repos can opt into and override,
  instead of hand-authoring full ruleset JSON in every repo.
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
│ │ ├── resolve_local.py
│ │ └── diff_rulesets.py
│ ├── templates/
│ │ ├── rmi-flow-main.json
│ │ ├── rmi-flow-prod.json
│ │ └── rmi-flow-next.json
│ ├── schema/
│ │ ├── config.schema.json
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
          config: .github/rulesets/config.json   # default value if omitted
```

- `config` input defaults to `.github/rulesets/config.json` inside the action's own
  `action.yml`, so most repos can omit `with:` entirely.
- No nested/object inputs — `with:` only carries scalar strings, per GitHub Actions
  input constraints. All structure lives in the config file on the consumer's side.

### 3.2 Consumer repo's `config.json`

Every consumer repo keeps an explicit config file, even if trivial — this keeps repo
intent visible in `git blame`/history rather than relying on invisible action defaults.

```json
{
  "rulesets": [
    { "template": "rmi-flow-main" },
    {
      "template": "rmi-flow-prod",
      "overrides": {
        "required_approving_review_count": 1
      }
    },
    { "template": "rmi-flow-next" },
    {
      "name": "custom-one-off",
      "ruleset": { "...": "full literal ruleset object, no template" }
    }
  ]
}
```

Two entry shapes are valid:
- `{ "template": "<name>", "overrides": { ... } }` — load bundled template, apply shallow
  overrides.
- `{ "name": "<name>", "ruleset": { ... } }` — fully literal, no template involved.
  Supported so no repo is forced to migrate to templates immediately.

## 4. Bundled templates

Location: `actions/admin/rulesets-check/templates/*.json`, one file per named template,
keyed by filename (`rmi-flow-main.json` → template name `rmi-flow-main`). These are plain
ruleset JSON objects in the same shape as what's currently hand-authored in each consumer
repo's `.github/rulesets/*.json` — minus the volatile fields already stripped today
(`id`, `current_user_can_bypass`, `_links`, `node_id`, `created_at`, `updated_at`,
`bypass_actors`).

Templates version with the action itself (same tag/SHA), so bumping `RMI/actions` to a
new template revision is a normal PR + release cycle there, and consumers pick it up on
their next run against the pinned ref.

## 5. Merge algorithm (template + overrides → resolved local ruleset)

**v1 scope: shallow, top-level merge only.**

```python
resolved = {**template, **entry.get("overrides", {})}
```

Explicitly *not* doing keyed/nested merging (e.g. overriding a single field inside one
object in the `rules[]` array) in v1 — array-merge semantics are ambiguous (by index? by
`rules[].type`?) and easy to get subtly wrong. Ship shallow-only first; only build
keyed-merge-by-`rules[].type` if a real case shows up that shallow can't express. This is
an additive change later — `overrides` staying flat vs. becoming structured doesn't break
existing config files.

## 6. Fetch / resolve / diff pipeline

Steps inside `actions/admin/rulesets-check/action.yml` (composite, no `concurrency:` —
each consumer's own caller workflow may add `concurrency:` at the job level if desired,
see §8):

1. **Fetch remote** (`scripts/fetch_remote.sh`, bash + `gh` + `jq` — unchanged from
   current logic): list ruleset IDs via `gh api /repos/{repo}/rulesets`, fetch each,
   strip volatile fields, write to `/tmp/remote_rulesets/<sanitized-name>.json`.

2. **Resolve local** (`scripts/resolve_local.py`, new): read the consumer's `config.json`
   (path from the `config` input), for each entry either:
   - load `templates/<template>.json` from `github.action_path` and shallow-merge
     `overrides`, or
   - use the literal `ruleset` object directly.
   Write each resolved ruleset to `/tmp/local_rulesets/<sanitized-name>.json` — same
   directory shape the diff step already expects, so downstream logic is unchanged
   regardless of whether a ruleset came from a template or was hand-authored.

3. **Diff** (`scripts/diff_rulesets.py`, new, replaces the current `diff --recursive`
   step): for each matching pair of remote/local JSON files:
   - Compute `tracked_keys = local.keys() & remote.keys()`.
   - Compute `new_keys = remote.keys() - local.keys()` (GitHub added a field).
   - Compute `removed_keys = local.keys() - remote.keys()` (GitHub removed/renamed a
     field, or local references a key that no longer exists remotely).
   - For `tracked_keys`: compare values; any mismatch → **fail** (`exit 1` at end of
     script, after processing all rulesets, so one bad ruleset doesn't hide others).
   - For `new_keys` / `removed_keys`: emit
     `::warning::Ruleset '<name>': key '<key>' <appeared in|missing from> remote API
     response — review and either track it or add to the ignore list` and append detail
     to `$GITHUB_STEP_SUMMARY`. **Do not fail** on these alone.
   - Rulesets present remotely but missing from local config (or vice versa) — i.e. no
     matching pair at all — treat as a separate warning class (`ruleset added/removed
     entirely`), not a per-key diff.

4. **Known-key list** (`schema/tracked-keys.json`): optional companion file listing keys
   the team has explicitly decided to *always* ignore even if new (e.g. a GitHub-added
   field nobody cares about yet). Consulted by `diff_rulesets.py` before classifying a
   key as "new" — if it's in the ignore list, skip silently rather than warning every
   run. Start empty; add entries as warnings get triaged.

## 7. Reusable-workflow wrapper (optional, decide later)

If the team decides it wants the "zero caller boilerplate" ergonomic (one line under
`jobs:` instead of a full job with checkout), add a thin `.github/workflows/admin/check-
rulesets.yml` in `RMI/actions` that itself calls the composite action:

```yaml
on:
  workflow_call:
    inputs:
      config:
        type: string
        default: .github/rulesets/config.json
jobs:
  rulesets:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: RMI/actions/actions/admin/rulesets-check@v1
        with:
          config: ${{ inputs.config }}
```

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
     confirm which map cleanly to `rmi-flow-main` / `-prod` / `-next` vs. need to stay
     literal (`ruleset` entries).
   - Write `.github/rulesets/config.json` accordingly.
   - Replace `.github/workflows/admin-check_rulesets.yml` contents with the thin caller
     from §3.1 (or §7's reusable-workflow form, if built).
   - Delete the old hand-authored ruleset JSON files once config.json covers them
     (keep literal ones that don't map to a template).
   - Confirm a run passes (or produces only expected warnings) before merging.

## 12. Open decisions to confirm during implementation

- Exact field lists for each of the three bundled templates (main/prod/next) — pull from
  current repos' existing JSON, reconcile differences.
- Initial contents (if any) of `schema/tracked-keys.json` ignore list.
- Whether to build the §7 reusable-workflow wrapper now or defer.
- Whether `resolve_local.py` / `diff_rulesets.py` should validate `config.json` against
  `schema/config.schema.json` up front (recommended: yes, fail fast with a clear error
  rather than a confusing downstream diff).
