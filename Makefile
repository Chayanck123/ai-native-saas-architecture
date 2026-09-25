# Developer and CI entry points. `make` (or `make help`) lists them.
#
# Everything runs through uv, so the only prerequisites are uv and, for the
# container / cluster targets, Docker, kustomize, kubeconform and terraform.

.DEFAULT_GOAL := help
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
MAKEFLAGS += --no-print-directory

SERVICES := gateway knowledge agents billing growth
IMAGE_REGISTRY ?= ainative
IMAGE_TAG ?= local

# Integration tests and the smoke test run against the Docker Compose PostgreSQL
# and Redis (`make deps-up`). The tests flush their Redis database, so they use
# a logical database (15) that the running platform never touches.
TEST_DATABASE_URL ?= postgresql+asyncpg://app:app@localhost:5432/app_test
TEST_REDIS_URL ?= redis://localhost:6379/15
ADMIN_DATABASE_URL ?= postgresql://postgres:postgres@localhost:5432/postgres

# AWS Lambda bundle: dependencies are resolved for the Lambda runtime, not for
# the machine running `make` - python3.12 runs on Amazon Linux 2023 (x86_64,
# glibc 2.34), so manylinux_2_28 wheels are compatible.
LAMBDA_DIR := infra/terraform/build/functions
LAMBDA_TARGET := --python-version 3.12 --python-platform x86_64-manylinux_2_28

CRD_SCHEMAS := https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json

.PHONY: help install lint fmt typecheck test check deps-up test-integration smoke demo serve \
	up down images package-lambdas k8s-validate tf-validate clean

help: ## List the available targets
	@grep -hE '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) \
		| awk -F':.*## ' '{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------ develop
install: ## Install every workspace package and the dev tools into .venv
	uv sync --all-packages

lint: ## Lint and check formatting (ruff)
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Format the code and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Static type checking (mypy --strict)
	uv run mypy

test: ## Unit and in-process tests: SQLite + in-memory bus, no services needed
	uv run pytest

check: lint typecheck test ## Everything that must pass before a commit

deps-up: ## Start only PostgreSQL (pgvector) and Redis in Docker
	docker compose up --detach --wait postgres redis

test-integration: deps-up ## The whole suite against real PostgreSQL (RLS, pgvector) and Redis
	DATABASE_URL='$(TEST_DATABASE_URL)' REDIS_URL='$(TEST_REDIS_URL)' uv run pytest

smoke: deps-up ## Every service as its own OS process, talking only via HTTP/SQL/Redis
	ADMIN_DATABASE_URL='$(ADMIN_DATABASE_URL)' REDIS_URL='$(TEST_REDIS_URL)' \
		uv run python scripts/smoke_distributed.py

# ---------------------------------------------------------------------- run
demo: ## Simulate five weeks of a SaaS business end to end (no Docker needed)
	uv run devstack demo

serve: ## The whole platform in one process: http://127.0.0.1:8000/docs
	uv run devstack serve

up: ## The distributed platform in Docker: gateway :8080, Jaeger :16686, Grafana :3000
	docker compose up --build --detach --wait

down: ## Stop the Docker platform (`make down V=1` also deletes its data)
	docker compose down $(if $(V),--volumes,)

# ------------------------------------------------------------------ release
images: ## Build one container image per service (IMAGE_REGISTRY, IMAGE_TAG)
	for service in $(SERVICES); do \
		docker build --file infra/docker/service.Dockerfile \
			--build-arg PACKAGE=$$service-service --build-arg SOURCE_DIR=services/$$service \
			--tag $(IMAGE_REGISTRY)/$$service:$(IMAGE_TAG) . ; \
	done
	docker build --file infra/docker/service.Dockerfile \
		--build-arg PACKAGE=serverless-functions --build-arg SOURCE_DIR=services/serverless \
		--tag $(IMAGE_REGISTRY)/functions:$(IMAGE_TAG) .

package-lambdas: ## Build the Lambda bundle Terraform deploys (infra/terraform/build)
	rm -rf '$(LAMBDA_DIR)'
	mkdir -p '$(LAMBDA_DIR)'
	uv export --frozen --no-dev --no-editable --no-hashes --no-emit-workspace \
		--package serverless-functions --output-file '$(LAMBDA_DIR)/requirements.txt'
	uv pip install --quiet --target '$(LAMBDA_DIR)' $(LAMBDA_TARGET) --only-binary :all: \
		--requirements '$(LAMBDA_DIR)/requirements.txt'
	uv pip install --quiet --target '$(LAMBDA_DIR)' $(LAMBDA_TARGET) --no-deps \
		packages/platform-core services/serverless
	rm '$(LAMBDA_DIR)/requirements.txt'
	@# Import every handler the way Lambda will: only the bundle plus the stdlib.
	uv run --no-project --python 3.12 -- python -B -I -S -c "import sys; \
		sys.path.insert(0, '$(LAMBDA_DIR)'); \
		import serverless_functions.stripe_webhook, serverless_functions.telemetry_beacon, \
		serverless_functions.scheduler"
	@echo "Lambda bundle: $$(du -sh '$(LAMBDA_DIR)' | cut -f1) in $(LAMBDA_DIR)"

k8s-validate: ## Render the Kustomize base and overlays; validate against the API schemas
	for dir in infra/k8s/base infra/k8s/overlays/*/; do \
		echo "--- $$dir"; \
		kustomize build "$$dir" | kubeconform -strict -summary \
			-schema-location default -schema-location '$(CRD_SCHEMAS)'; \
	done
	@echo "--- infra/k8s/jobs (templates, with example values)"
	sed -E 's/\$$\{[A-Z_]+\}/example/g' infra/k8s/jobs/*.yaml | kubeconform -strict -summary \
		-schema-location default -schema-location '$(CRD_SCHEMAS)'

tf-validate: ## Terraform formatting and validation (no AWS credentials needed)
	terraform -chdir=infra/terraform fmt -check -recursive
	terraform -chdir=infra/terraform init -backend=false -input=false >/dev/null
	terraform -chdir=infra/terraform validate

clean: ## Delete caches, local databases and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache data infra/terraform/build \
		infra/terraform/modules/serverless/.build
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +
