# forbidden-patterns (composite action)

Fail a workflow if any forbidden strings or regex patterns are found with `git grep` (tracked files only). Emits per-line GitHub **annotations** and a clean **Summary** table before failing.

## Usage

```yaml
name: forbidden strings

on:
  pull_request:
  push:
    branches: [main, next, production]

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5

      - name: Forbidden patterns
        uses: RMI/actions/actions/admin/forbidden-patterns@main
        with:
          # Merge shipped defaults (conflict markers, TODO/FIXME/XXX, etc.)
          default_patterns: true

          # Add your repo-specific patterns (one per line)
          patterns: |
            \bconsole\.log\(
            \bdebugger\b
            (^|[^[:alpha:]])TKTK?([^[:alpha:]]|$)

          # Or keep patterns in-repo and point to them:
          # patterns_file: .github/forbidden.txt

          # Matching mode: "regex" (git grep -E) or "literal" (-F)
          mode: regex

          # Case-insensitive by default
          ignore_case: true

          # Optional path globs (newline-delimited). If omitted, scan the whole repo.
          include: |
            **/*.ts
            **/*.tsx
            **/*.md
          exclude: |
            src/**/*.test.ts
            src/**/*.test.tsx
            public/schema.html

          # Cap annotations (GitHub has limits)
          max_annotations: 200
```
