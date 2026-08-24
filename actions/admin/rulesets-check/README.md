# rulesets-check (composite action)

Compare a repository's **live GitHub rulesets** against **templated definitions kept in
the repo**, so nobody can quietly change branch protections in the GitHub UI without the
change showing up in CI.

- **Allow-list model:** the resolved template+overlay *is* the allow-list — the check
  compares exactly the keys you define, nothing else.
- A tracked key whose value differs on the live ruleset, or is missing from it → **fail**.
- Keys GitHub returns that you don't define (volatile IDs, new schema fields) → ignored.
  Genuinely new schema properties are caught centrally by the [nightly coverage
  check](#nightly-schema-coverage-check), not by nagging every consumer PR.
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
    uses: RMI/actions/.github/workflows/admin-check-rulesets.yml@main
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

## What gets checked (allow-list model)

The check is **local-driven**: the resolved template+overlay defines exactly which keys are
compared. For every key you define:

- present on the live ruleset with a different value → **fail**;
- missing from the live ruleset → **fail** (the remote doesn't enforce something you track);
- keys the live ruleset has that you *don't* define (`id`, `source`, timestamps, a
  brand-new GitHub field) → **ignored**.

So there's no strip list and no ignore list to maintain — a field is checked *iff* a
template or overlay sets it. `fetch_remote.sh` dumps raw API responses; the diff simply
never looks at keys you didn't define.

**To start (or stop) tracking a key:**

| You want to… | Do this |
|---|---|
| Enforce a key's value across all repos | Add it to the relevant **template** (`templates/*.json`) — propagates to every consumer on their next run |
| Enforce a key for one repo only | Add it to that repo's **overlay** |
| Never track a key (silence the nightly coverage check for it) | Add its dotted path to `schema/acknowledged-untracked.json` |

## Reading a failure

Each drift failure reports the exact dotted path and both values, in three places:

- the **step log** — a full block per failure (`local = …` / `remote = …`);
- a **check annotation** — a one-line, value-bearing summary (e.g.
  `rules.pull_request.parameters.require_last_push_approval differs — local=true remote=false`);
- the **job summary** — the same detail in a table.

For the whole picture, each *failing* ruleset's resolved-local and raw live JSON are dumped
in a collapsed `::group::diagnostics: <name>` block in the log. To dump **every** ruleset
(including passing ones — useful when a check passes but you expected a failure), re-run the
job with **debug logging enabled** (GitHub sets `RUNNER_DEBUG=1`).

## Nightly schema-coverage check

Because the per-PR check is quiet about fields you don't define, a separate **nightly**
workflow (`.github/workflows/admin-rulesets-schema-check.yml` + `scripts/schema_coverage.py`)
compares GitHub's published `repository-ruleset` schema against what the templates cover. The
run **fails** on drift either way — a property that is **neither tracked nor acknowledged**
(GitHub added a field), or a key the templates track that the schema **no longer lists** (a
rename/removal, which would otherwise start failing every consumer PR). The failed scheduled
run is the alert (it opens no issue); the step log and job summary list exactly what drifted.
That's the one place drift in GitHub's schema surfaces — centrally, once — instead of on
every consumer PR.

`schema/acknowledged-untracked.json` holds the dotted paths you've deliberately decided not
to track (e.g. `id`, `source`, `bypass_actors`), so they don't fail the nightly run. Coverage
scope for v1 is top-level ruleset properties + rule types + rule parameters; `conditions`
internals are compared as a whole (documented follow-up to deepen).
