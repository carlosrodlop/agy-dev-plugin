# GitHub Copilot Code Review Instructions (`agy-dev-plugin`)

Review pull requests against these mandatory engineering guardrails. Flag violations concretely with file and line references.

## 1. Security, Privacy & Zero-PII
- **Zero-PII & Zero-Secrets**: Reject customer names, personal emails, tokens, credentials, private keys, or hardcoded local `/Users/<user>/` paths in tracked files. Use synthetic mocks only (`AcmeCorp`, `acme/repo-a`); keep live secrets in `0600` `~/.env` (mirrored in `.env.example`).
- **Protected `main`**: Never suggest direct pushes or merges to `main`/`master`; route changes through timestamped `fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>` branches under per-repo `dev_pr_locks`.

## 2. VCS-Agnostic Native Git / SSH Only
- **No Proprietary VCS CLIs or Forge Tokens**: NEVER suggest or introduce `gh`, `glab`, `hub`, or GitHub/GitLab REST API tokens (`PROPRIETARY_VCS_CLI_RE` in `clients/dev_common.py`). Use native `/usr/bin/git` and `/usr/bin/ssh` (`clients/git_client.py`) exclusively.

## 3. Santa Lockdown & Declarative Tooling
- **Standard Languages Only**: Python 3 + POSIX/Bash only.
- **No Unapproved Binaries**: Reject `curl | sh`, `wget | sh`, `mise`, or ad-hoc compiled binary downloads. Declare runtimes via `asdf` (`.tool-versions`) and dependencies in `pyproject.toml`.

## 4. CI, Portability & Code Hygiene
- **Pinned CI**: GitHub Actions workflows MUST pin `runs-on: ubuntu-24.04` (never `ubuntu-latest`) and avoid duplicate `push:` triggers when `pull_request:` already runs.
- **Python Safety & Portability**: Forbid bare `except:` clauses and mutable default arguments (`[]`, `{}`, `set()`). Keep code Unix-portable (`macOS` & `Linux`); gate macOS-only calls behind `platform.system() == "Darwin"`.
- **English Only**: Code, comments, docstrings, identifiers, and documentation must be in English.

## 5. Instruction Size Caps & Hermetic Testing
- **Size Ceilings**: Keep `rules/*.md`, `agents/*.md`, and `skills/*/SKILL.md` strictly `< 24,000` bytes, and `.github/copilot-instructions.md` strictly `<= 4,000` bytes.
- **Hermetic Offline Tests**: Add or update 100% hermetic offline unit tests (`unittest` + `bats`) with zero network calls or live credentials.
