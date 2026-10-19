# Working in this repository

## Deploy config and app code go in separate PRs

A pull request changes deploy config (`compose*.y*ml`, `.env*`, `Dockerfile*`, `deploy/`, `k8s/`, `infra/`, `.github/workflows/`) **or** app code (`miragen/`, `miragen_hook/`, `packages/`, `plugins/`; tests and docs may go with either), never both. The release controller's automatic rollback reverts only the app image, and a redeploy re-reads compose and env from main, so a mixed PR cannot be undone by rolling back. Put the config change in its own small PR. The `pr-scope` CI check (`scripts/ci/pr_scope.py`) enforces this; when a change truly cannot be split, a person adds the `mixed-change-ok` label and the check re-runs.
