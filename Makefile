.PHONY: help install qa test test-fast test-bats ci-local pre-commit clean

PYTHON ?= python3

help:
	@echo "agy-dev-plugin targets:"
	@echo "  make install    - Deploy plugin bundle into $$HOME/.gemini/config/plugins/agy-dev-plugin"
	@echo "  make qa         - Run static AST (including F821), Zero-PII, shell, and plugin size audit"
	@echo "  make test       - Run full offline unit test suite and BATS checks"
	@echo "  make test-fast  - Run selective unit tests against changed files"
	@echo "  make ci-local   - Run full GitHub Actions CI parity locally (QA + coverage >=75% + BATS)"
	@echo "  make pre-commit - Run pre-commit hooks across all repository files"

install:
	@./install.sh

qa:
	@$(PYTHON) ./tests/scripts/qa_static_audit.py --strict

test-fast:
	@$(PYTHON) ./tests/scripts/selective_test_runner.py

test-bats:
	@if command -v bats >/dev/null 2>&1; then bats tests/bats/*.bats; else echo "bats not installed; skipping"; fi

test: qa
	@$(PYTHON) -m unittest discover -s tests -p "test_*.py" -v
	@$(MAKE) test-bats

ci-local: qa
	@AGY_OFFLINE_ONLY=1 PRE_COMMIT_HOOK=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. $(PYTHON) -m coverage run --source=clients,skills,sidecars,tests/scripts -m unittest discover -s tests -p "test_*.py"
	@$(PYTHON) -m coverage report --show-missing --fail-under=75
	@$(PYTHON) ./tests/scripts/selective_test_runner.py --audit-integrity
	@$(MAKE) test-bats

pre-commit:
	@pre-commit run --all-files

clean:
	@find . -name "__pycache__" -type d -prune -exec rm -rf {} +
	@rm -rf .pytest_cache htmlcov .coverage
