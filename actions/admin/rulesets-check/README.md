# rulesets-check (composite action)

Compare a repository's **live GitHub rulesets** against **templated definitions kept in
the repo**, so nobody can quietly change branch protections in the GitHub UI without the
change showing up in CI.

- **Value mismatch** on a tracked key → **fail** the job.
- **Schema drift** (a key GitHub added or removed) → **warn**, don't fail.
- Emits GitHub annotations and a job-summary table.

Templates (the RMI gitflow defaults) ship *with* this action, so consumer repos keep only a
tiny **overlay** file per ruleset instead of hand-authoring full ruleset JSON.

## Usage

### Direct (composite action)

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
          rulesets_dir: .github/rulesets   # default; omit to accept it
```

### Via the reusable workflow

```yaml
jobs:
  rulesets:
    uses: RMI/actions/.github/workflows/admin/check-rulesets.yml@main
```

### Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `rulesets_dir` | `.github/rulesets` | Directory of `*.overlay.json` files |
| `repository` | current repo | `owner/name` to check |
| `github_token` | `github.token` | Token for `gh api` ruleset reads (needs repo admin) |

## Overlay files

Put one `*.overlay.json` per ruleset in `rulesets_dir`. Every overlay **must** name its
`template`; an overlay holds **only** what this repo changes on top of it, so
`{ "template": "gitflow-main" }` means "take the `gitflow-main` template verbatim".

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

**Required status checks are per-repo.** Templates ship an empty contexts list; each repo
supplies its own:

```json
{
  "template": "gitflow-main",
  "rules": {
    "required_status_checks": {
      "parameters": {
        "required_status_checks": [
          { "context": "Tests / tests (pytest)", "integration_id": 15368 }
        ]
      }
    }
  }
}
```

**Fully custom rulesets** — for a repo-specific ruleset that maps to no gitflow template,
use the `blank` template and author the whole thing in the overlay (rules as a
`{type: patch}` map, same as everywhere else):

```json
{
  "template": "blank",
  "name": "code-quality-copilot-review",
  "conditions": { "ref_name": { "include": ["~ALL"], "exclude": [] } },
  "rules": { "copilot_code_review": {} }
}
```

There is one mechanism — template + overlay. `blank` is just a minimal template
(`target: branch`, `enforcement: active`, no rules) to build on.

## Bundled templates

`templates/*.json` — canonical rulesets (`rules` as a list), volatile and identity fields
stripped:

| Template | Notes |
|----------|-------|
| `gitflow-main` | `dismiss_stale_reviews_on_push` + `require_last_push_approval` = true; 1 approval; empty status-check contexts |
| `gitflow-production` | 2 approvals; empty status-check contexts |
| `gitflow-next-lifecycle` | includes `refs/heads/{next,hotfix/**,demo/**}` |
| `gitflow-next-pr` | PR rules for `next` |
| `blank` | minimal base (`target: branch`, `enforcement: active`, no rules) for fully custom rulesets |

## Merge semantics (`resolve_local.py`)

Overlay is deep-merged onto the template:

1. **Objects** → recurse; overlay leaf scalars win.
2. **`rules`** → keyed by `.type`. The overlay writes rules as a **`{type: patch}` map**;
   each patch is deep-merged onto the matching template rule. `null` removes a rule; an
   unknown type is added. Output re-emits `rules` as a canonical list.
   **At most one rule per `type`** — a duplicate is a hard error.
3. **All other arrays** (`conditions.ref_name.include`, status-check contexts) → **replaced
   wholesale**.

Validation is enforced inline (missing/unknown `template`, missing `name`, duplicate rule
types, invalid JSON) — no external `jsonschema` dependency at runtime.
`schema/overlay.schema.json` is provided for editor tooling.

## `schema/diff-config.json` — stripping & ignoring

One file drives what the diff ignores, applied to **both** sides:

- **`strip`** — top-level fields removed entirely before comparing (never a drift signal):
  `id`, `node_id`, `_links`, `created_at`, `updated_at`, `current_user_can_bypass`,
  `bypass_actors`, `source`, `source_type`. `bypass_actors` is out of scope for now
  (security-relevant but noisy); `source`/`source_type` are per-repo identity.
- **`ignore`** — dotted key paths whose new/removed-key drift is silenced once triaged,
  e.g. `rules.pull_request.parameters.some_new_github_field`.

`fetch_remote.sh` dumps raw API responses; all stripping happens here so there's a single
source of truth.
