# Development Workflow

## Branching model

`main` is always runnable and only receives merges from short-lived branches. Nobody commits to it
directly; the only exception is the initial bootstrap commit.

| Prefix | Use | Example |
|---|---|---|
| `feature/` | New capability (one plan phase or sub-phase) | `feature/repo-ingestion` |
| `fix/` | Bug fix | `fix/zip-symlink-check` |
| `docs/` | Documentation only | `docs/implementation-plan` |
| `test/` | Test-only work | `test/e2e-acceptance` |
| `chore/` | Tooling, CI, dependencies, hardening | `chore/hardening` |

Branch names for each phase are listed in [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md).

## Lifecycle of a branch

```bash
git switch main && git pull --ff-only          # start from the latest main
git switch -c feature/<name>                    # one branch per feature / phase slice
# ... small, focused commits ...
make check                                      # ruff + mypy + pytest (once Phase 1 lands)
git switch main && git merge --no-ff feature/<name> -m "Merge feature/<name>: <summary>"
git push origin main                            # when remote pushing is enabled
git branch -d feature/<name>
```

We merge with `--no-ff` so that every feature shows up in history as one unit. When the GitHub remote
is used for review, open a PR from the branch instead of merging locally. The same rules apply.

## Merge checklist

A branch merges only when:

1. The phase's **exit criteria** in the plan are met and verified (tests or a recorded run).
2. `ruff check`, `mypy`, and `pytest` pass locally (and in CI once it exists).
3. No secrets, generated artifacts, `data/`, or `.env` files are staged.
4. Docs touched by the change are updated, including the plan checklist and progress log.
5. Nothing reports a fabricated metric, and no route is a placeholder.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/) style:

```
<type>(<scope>): <imperative summary>

<optional body: what and why>
```

Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `chore`, `ci`, `build`.
Scopes follow package names: `ingestion`, `indexing`, `audit`, `ai`, `sandbox`, `profiling`,
`benchmark`, `reporting`, `api`, `ui`, `db`.

Examples:

```
feat(ingestion): reject zip entries that escape the workspace
test(audit): add false-positive cases for list-membership rule
docs(plan): tick Phase 2 ingestion checklist
```
