.PHONY: help install qa test test-fast test-bats pre-commit clean

PYTHON ?= python3

help:
	@echo "agy-dev-plugin targets:"
	@echo "  make install    - Deploy plugin bundle into $$HOME/.gemini/config/plugins/agy-dev-plugin"
	@echo "  make qa         - Run static AST, Zero-PII, shell, and plugin size audit"
	@echo "  make test       - Run full offline unit test suite and BATS checks"
	@echo "  make test-fast  - Run selective unit tests against changed files"
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

pre-commit:
	@pre-commit run --all-files

clean:
	@find . -name "__pycache__" -type d -prune -exec rm -rf {} +
	@rm -rf .pytest_cache htmlcov .coverage
