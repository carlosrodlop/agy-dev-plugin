---
trigger: glob
globs: "**/*.sh, **/*.bash, **/*.zsh, **/*.bats, **/Makefile"
description: POSIX, Bash, and Zsh software engineering best practices, macOS/Linux portability, Santa Lockdown binary constraints, strict-mode guards, and BATS testing.
---

# Shell & Makefile Engineering Standards (`shell-standards.md`)

## 1. Strict Mode & Sourced-Script Guard
- **Standalone Executables**: Start executable scripts with `#!/usr/bin/env bash` (or `#!/usr/bin/env sh` for strict POSIX) and enable `set -euo pipefail` immediately after the header.
- **Sourced Libraries & Dual-Mode Wrappers**: **NEVER** place top-level `set -euo pipefail` in scripts intended to be `source`d into an interactive shell (it will terminate the user's terminal on any non-zero return). Detect sourcing via `BASH_SOURCE[0]` / `ZSH_EVAL_CONTEXT` and enable `set -euo pipefail` strictly inside the direct-execution branch.
- **Clean Exit Semantics**: Return explicit exit codes (`0` success, `1` runtime error, `2` CLI usage error) and write diagnostics to `stderr` (`>&2`), keeping `stdout` clean for machine-readable piping.

## 2. Quoting, Scoping & Temporary File Hygiene
- **Quote Every Expansion**: Always double-quote variable and command expansions (`"${var}"`, `"$(cmd)"`) to survive spaces, globs, and word splitting. Use `${var:-}` defaults when checking possibly unset variables under `set -u`.
- **Safe Destructive Operations**: Never run unquoted destructive commands (`rm -rf $VAR`). Guard path deletions with `${VAR:?}` (e.g., `rm -rf "${TARGET_DIR:?}/${item}"`) and verify non-empty paths first.
- **Local Scope & `printf`**: Declare function variables with `local` and prefer `printf '%s\n' "${val}"` over `echo` (which differs across BSD/GNU shells on `-n`, `-e`, and backslashes).
- **Temp Files & Traps**: Create temporary files/directories with `mktemp` (portable template syntax) and register `trap 'rm -rf "${tmp_dir:-}"' EXIT INT TERM` immediately so interrupted runs never leak temp artifacts or secrets (`chmod 700` / `600`).

## 3. Cross-Platform Portability (`macOS` BSD vs. `Linux` GNU)
- **Avoid GNU-Only Flags**: Do not rely on GNU-only extensions that fail on macOS BSD userland (`sed -i ''` vs `sed -i`, `date -d`, `readlink -f`, `grep -P`, `xargs -r`).
- **Delegate Non-Trivial Logic to Python 3**: Keep shell scripts thin (bootstrapping, env wiring, process launching). Delegate JSON parsing, date math, URL encoding, atomic file locking, and complex text processing to `python3` stdlib rather than fragile `sed`/`awk` chains.
- **Zero Hardcoded User Paths**: Parameterize paths via `"${HOME}"` and `"${SCRIPT_DIR}"` (resolved from `BASH_SOURCE[0]`), never `/Users/<username>/`.

## 4. Santa Lockdown Binary & Workstation Safety
- **Zero Unapproved Binary Downloads**: Under Google Santa Lockdown on corporate workstations, local scripts (`install.sh`, `Makefile`, `shell/*.sh`, local `pre-commit` hooks) MUST NEVER:
  - Pipe remote content into shells or interpreters (`curl|sh`, `wget|sh`, `curl|python3`, `wget|python`).
  - Download standalone compiled `Mach-O` / `ELF` binaries from external URLs or GitHub Releases (`releases/download/` + `chmod +x`).
  - Invoke unapproved binary managers (`mise install/use`, `uv tool install`, `deno`, `bun`, `cargo binstall`).
- **Approved Toolchain Hierarchy**: Manage runtimes declaratively via pure-script `asdf` (`.tool-versions` + `.tool-plugins`), Python packages via `pyproject.toml`, and rely on OS-signed / corporate-approved utilities (`/usr/bin/git`, `/usr/bin/ssh`, `/usr/bin/curl`, `/usr/bin/sqlite3`, `jq system`).
- **Local vs. CI Separation**: Keep workstation scripts (`install.sh`, `Makefile`) 100% free of binary downloads. Ephemeral Linux CI runners (`ubuntu-24.04` in `.github/workflows/*.yml`) may install pinned OS distro packages when needed (`apt-get install -y bats`).

## 5. Validation & Hermetic BATS Testing
- **Static Syntax & Audit**: Validate every `.sh`/`.bash` file with `bash -n`, every zsh-compatible script with `zsh -n` when applicable, and run [`project_audit.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py).
- **Hermetic BATS Suites**: Cover installers and CLI wrappers with offline tests in `tests/bats/*.bats` using isolated temp `$HOME` directories (`BATS_TEST_TMPDIR`) and stripping inherited git hook env vars (`env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_COMMON_DIR -u GIT_PREFIX bats tests/bats/*.bats`).
