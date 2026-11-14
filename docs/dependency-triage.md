# Dependency triage process

## Weekly (30 min, every Monday)

1. `gh pr list --repo ninelegsdog/<repo> --state open` for each botkit-* repo.
2. Classify each open PR:
   - **Safe** (minor/patch bump, github_actions, codeql): auto-merge if check green.
   - **Hold** (major bump, base image, python slim): leave open with `hold` label.
   - **Stale** (branch deleted, conflicts): close, dependabot recreates.
   - **Redundant** (chore PR duplicating existing files): close as redundant.
3. Close stale/redundant PRs with a comment explaining why.
4. Update held PRs if a new version is available.
5. Log summary: merged/closed/held counts.

## Auto-merge rules

- Safe bumps (semver patch/minor, github_actions, codeql): auto-merge via
  `.github/workflows/dependabot-auto-merge.yml`.
- Major bumps (python 3.x, base image): hold for manual review.
- Farm offline-run failure alone does not block merge (bot's own check is required).

## Hold criteria

A PR is held if:
- It bumps a major version (python 3.13 -> 3.15).
- It changes a Docker base image digest.
- The bot's own `check` fails (not just farm offline-run).

## Stale PR criteria

A PR is stale if:
- Its head branch is deleted (404 on branches API).
- It has merge conflicts (DIRTY).
- A newer version of the same dependency is available.
