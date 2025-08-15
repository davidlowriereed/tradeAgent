# Quick diagnostics playbook
.PHONY: diag smoke audit

BASE ?= http://localhost:8080

diag: audit smoke

smoke:
	bash diagnostics/scripts/run_smoke_tests.sh "$(BASE)"

audit:
	python3 diagnostics/scripts/import_audit.py --repo . --base-url "$(BASE)"
