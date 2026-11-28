# GitHub Copilot Code Review Instructions (`agy-dev-plugin`)

Review pull requests against these mandatory engineering guardrails. Flag violations concretely with file and line references.

## 1. Security, Privacy & Zero-PII
- **Zero-PII & Zero-Secrets**: Reject customer names, personal emails, tokens, credentials, private keys, or hardcoded local `/Users/<user>/` paths in tracked files. Use synthetic mocks only (`AcmeCorp`, `acme/repo-a`); keep live secrets in `0600` `~/.env` (mirrored in `.env.example`).
- **Protected `main`**: Never suggest direct pushes or merges to `main`/`master`; route changes through timestamped `fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>` branches under per-repo `dev_pr_locks`.

## 2. GitHub-First Native Git / SSH (Zero `gh` Binary)
- **Santa-Safe GitHub Transport**: NEVER suggest `gh`, `glab`, or `hub` binaries (`PROPRIETARY_VCS_CLI_RE` in `clients/dev_common.py`). Use native `/usr/bin/git` + `/usr/bin/ssh` (`clients/git_client.py`); any GitHub REST need uses Python stdlib `urllib.request` only.

## 3. Santa Lockdown & Declarative Tooling
- **Primary Languages**: Python 3, POSIX/Bash Shell, and Vanilla-First Zero-Build Web (`ES2022+` JS/HTML5/CSS3; frameworks only if explicitly declared).
- **No Unapproved Binaries**: Reject `curl|sh`, `curl|python*`, `releases/download` + `chmod +x`, `mise`, `uv tool install`, `cargo binstall`, and native binary bundlers. Pin runtimes in `.tool-versions` (`asdf`) and pure-Python deps in `pyproject.toml`.

## 4. CI, Portability & Code Hygiene
- **Pinned CI**: Pin `runs-on: ubuntu-24.04` (never `ubuntu-latest`); avoid duplicate `push:` triggers alongside `pull_request:`.
- **5-Language Safety & Portability**: Forbid bare `except:`, `eval`/`exec`, unsafe `yaml.load`, mutable defaults (`[]`, `{}`, `set()`), JS `eval`/`new Function`/`document.write`/`javascript:`, duplicate HTML IDs, and unbalanced CSS braces. Keep Unix-portable (`macOS`/`Linux`); gate Darwin calls behind `platform.system() == "Darwin"`.
- **English Only**: Code, comments, docstrings, identifiers, and documentation must be in English.

## 5. Instruction Size Caps & Hermetic Testing
- **Size Ceilings**: Keep `rules/*.md`, `agents/*.md`, and `skills/*/SKILL.md` strictly `< 24,000` bytes, and `.github/copilot-instructions.md` strictly `<= 4,000` bytes.
- **Hermetic Offline Tests**: Add or update 100% hermetic offline unit tests (`unittest` + `bats`) with zero network calls or live credentials.
