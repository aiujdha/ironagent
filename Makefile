.PHONY: dev admin test test-unit lint format build clean seed migrate migrate-new migrate-json-to-mysql ironagent-image up ps down check check-python check-go check-specs check-k8s-render check-k8s-preflight check-hygiene check-naming-residuals deploy-preflight

IRON_AGENT_REPO_PATH ?= ../ironagent-main
IRON_AGENT_IMAGE_TAG ?= ironagent-runtime:dev
K8S_OVERLAY ?= deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport
GO ?= $(shell if command -v go >/dev/null 2>&1; then command -v go; elif [ -x /opt/homebrew/bin/go ]; then echo /opt/homebrew/bin/go; else echo go; fi)
KUBECTL ?= $(shell if command -v kubectl >/dev/null 2>&1; then command -v kubectl; elif [ -x /usr/local/bin/kubectl ]; then echo /usr/local/bin/kubectl; else echo kubectl; fi)
IRON_AGENT_STACK_ROOT ?= $(CURDIR)
IRON_AGENT_SECURITY_SERVICE_PORT ?= 8000
IRON_AGENT_ADMIN_PORT ?= 8501
IRON_AGENT_CONTROL_UI_PUBLIC_URL ?= http://127.0.0.1:18789
IRON_AGENT_SECURITY_SERVICE_PUBLIC_URL ?= http://127.0.0.1:$(IRON_AGENT_SECURITY_SERVICE_PORT)
ifeq ($(origin IRON_AGENT_RUNTIME_SOURCE_ROOT), undefined)
IRON_AGENT_RUNTIME_SOURCE_ROOT := $(shell \
	for path in \
		../ironagent-main \
		../ironagent-main-main \
		../ironagent-main-feat-webui \
		../ironagent-main-feat-webui-cleancheck \
		../ironagent-main-feat-webui-online \
		../ironagent-main-feat-comfier \
		../ironagent-main-feat-comfier-prev \
		$$HOME/Downloads/ironagent-main \
		$$HOME/work/ironagent-main; do \
		if [ -f "$$path/package.json" ]; then \
			cd "$$path" && pwd; \
			break; \
		fi; \
	done \
)
endif
COMPOSE_ENV = IRON_AGENT_STACK_ROOT="$(IRON_AGENT_STACK_ROOT)" IRON_AGENT_RUNTIME_SOURCE_ROOT="$(IRON_AGENT_RUNTIME_SOURCE_ROOT)" IRON_AGENT_SECURITY_SERVICE_PORT="$(IRON_AGENT_SECURITY_SERVICE_PORT)" IRON_AGENT_ADMIN_PORT="$(IRON_AGENT_ADMIN_PORT)" IRON_AGENT_CONTROL_UI_PUBLIC_URL="$(IRON_AGENT_CONTROL_UI_PUBLIC_URL)" IRON_AGENT_SECURITY_SERVICE_PUBLIC_URL="$(IRON_AGENT_SECURITY_SERVICE_PUBLIC_URL)"

define require-compose-env
	@test -n "$(IRON_AGENT_RUNTIME_SOURCE_ROOT)" || (echo "Set IRON_AGENT_RUNTIME_SOURCE_ROOT to an absolute IronAgent repo path before running Docker Compose targets." && exit 1)
endef

dev:
	uv run uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

admin:
	uv run uvicorn src.admin_web.app:app --host 0.0.0.0 --port 8501

test:
	uv run pytest tests/ -v --tb=short

test-unit:
	uv run pytest tests/unit/ -v

lint:
	uv run ruff check src tests scripts
	uv run mypy src

format:
	uv run ruff format src tests scripts

check: check-python check-go check-specs check-k8s-render check-hygiene

check-python:
	@if command -v uv >/dev/null 2>&1; then \
		$(MAKE) lint; \
		$(MAKE) test; \
	elif [ -x .venv/bin/python ] && [ -x .venv/bin/ruff ]; then \
		.venv/bin/ruff check src tests scripts; \
		if [ -x .venv/bin/mypy ]; then .venv/bin/mypy src; else echo "WARN: .venv/bin/mypy not found; skipping mypy"; fi; \
		.venv/bin/python -m pytest tests/ -v --tb=short; \
	else \
		echo "ERROR: uv or .venv Python toolchain is required for check-python"; \
		exit 1; \
	fi

check-go:
	cd go-gateway && "$(GO)" test -count=1 ./...

check-specs:
	@if [ -f scripts/spec_lint.py ]; then \
		if [ -x .venv/bin/python ]; then \
			.venv/bin/python scripts/spec_lint.py; \
		elif command -v uv >/dev/null 2>&1; then \
			uv run python scripts/spec_lint.py; \
		elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then \
			python3 scripts/spec_lint.py; \
		else \
			echo "WARN: neither uv nor Python >=3.11 found; skipping spec lint"; \
		fi; \
	else \
		echo "WARN: scripts/spec_lint.py not found; skipping spec lint"; \
	fi

check-k8s-render:
	@if command -v "$(KUBECTL)" >/dev/null 2>&1 || [ -x "$(KUBECTL)" ]; then \
		"$(KUBECTL)" kustomize "$(K8S_OVERLAY)" >/tmp/ironagent-k8s-render.yaml; \
	else \
		echo "WARN: kubectl not found; skipping k8s render"; \
	fi

check-k8s-preflight:
	@if [ -f scripts/k8s/preflight_deploy.sh ]; then \
		KUBECTL="$(KUBECTL)" bash scripts/k8s/preflight_deploy.sh "$(K8S_OVERLAY)"; \
	else \
		echo "ERROR: scripts/k8s/preflight_deploy.sh not found"; \
		exit 1; \
	fi

check-hygiene:
	git diff --check
	@! rg -n '^(<<<<<<<|=======|>>>>>>>)' .

check-naming-residuals:
	@if [ -x scripts/check_naming_residuals.py ]; then \
		scripts/check_naming_residuals.py; \
	else \
		python3 scripts/check_naming_residuals.py; \
	fi

deploy-preflight: check-k8s-preflight

build:
	$(call require-compose-env)
	$(COMPOSE_ENV) docker compose build

up:
	$(call require-compose-env)
	$(COMPOSE_ENV) docker compose up -d --build security-service admin

ps:
	$(call require-compose-env)
	$(COMPOSE_ENV) docker compose ps

down:
	$(call require-compose-env)
	$(COMPOSE_ENV) docker compose down

ironagent-image:
	./scripts/build_ironagent_image.sh "$(IRON_AGENT_REPO_PATH)" "$(IRON_AGENT_IMAGE_TAG)"

clean:
	$(COMPOSE_ENV) docker compose down -v || true
	find . -type d -name __pycache__ -exec rm -rf {} +

seed:
	uv run python scripts/seed.py

migrate:
	uv run alembic upgrade head

migrate-new:
	uv run alembic revision --autogenerate -m "$(msg)"

migrate-json-to-mysql:
	uv run python scripts/migrate_json_to_mysql.py
