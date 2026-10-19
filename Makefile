TARGET  := $(CURDIR)/target
VENV    := $(CURDIR)/.venv
PYTHON  := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
UV      := $(VENV)/bin/uv
UV_CACHE_DIR := $(TARGET)/.uv-cache
UV_RUN  := UV_CACHE_DIR=$(UV_CACHE_DIR) $(UV)

PY_PACKAGE := opentext_kb
CLI        := opentext-kb-dev

ENV_FILE ?= $(CURDIR)/config/.env

BASELINE_DIR ?= $(CURDIR)/.baseline
BANDIT_BASELINE_FILE ?= $(BASELINE_DIR)/bandit-baseline.json
DETECT_SECRET_BASELINE_FILE ?= $(BASELINE_DIR)/detect-secret-baseline.json
DETECT_SECRET_EXCLUDE_FILES ?= detect-secret-baseline\.json
BASEDPYRIGHT_BASELINE_FILE ?= $(BASELINE_DIR)/basedpyright-baseline.json
BANDIT_IGNORED_RULES ?= B101,B108

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ── Bootstrap ──────────────────────────────────────────────────────────────────

$(TARGET)/.venv-created:
	@mkdir -p $(TARGET)
	python3 -m venv $(VENV)
	@touch $@

$(TARGET)/.uv-installed: $(TARGET)/.venv-created
	$(PIP) install --upgrade pip setuptools wheel
	$(PIP) install uv
	@touch $@

# Install published dependencies; no sibling checkout is needed.
$(TARGET)/.compiled: pyproject.toml uv.lock $(TARGET)/.uv-installed
	VIRTUAL_ENV= $(UV_RUN) sync --locked --extra dev
	@touch $@

.PHONY: dev
dev: $(TARGET)/.compiled ## Install locked dependencies from PyPI
	@echo "Dependencies installed."

# fred's libraries: `pypi` (the locked, published ones) or `local` (your fred
# checkout at FRED_REPO, which has no default): fred-deployment-factory's
# docs/LOCAL-DEVELOPMENT.md, "Work on unreleased fred". `dev` is the only step
# that syncs (UV_NO_SYNC), so a `make dev-local` overlay survives every `uv run`.
FRED_LIBS ?= pypi
FRED_REPO ?=
export UV_NO_SYNC := 1

.PHONY: fred-repo-check
fred-repo-check:
	@test -n "$(FRED_REPO)" || { printf '%s\n' \
		"FRED_REPO is not set: give the path of YOUR fred checkout, e.g." \
		"    export FRED_REPO=/path/to/your/fred" \
		"(without it, make dev and FRED_LIBS=pypi use the released fred)" >&2; exit 1; }
	@test -d "$(FRED_REPO)/libs" || { echo "FRED_REPO=$(FRED_REPO) is not a fred checkout (no libs/): set FRED_REPO to the path of your fred checkout." >&2; exit 1; }

.PHONY: dev-local
dev-local: fred-repo-check dev ## Swap in fred's libraries from your fred checkout at FRED_REPO (editable)
	VIRTUAL_ENV=$(VENV) $(UV_RUN) pip install \
		--editable $(FRED_REPO)/libs/fred-pod \
		--editable "$(FRED_REPO)/libs/fred-sdk[knowledge-base]"
	@echo "fred's libraries from $(FRED_REPO) ($$(git -C $(FRED_REPO) describe --always --dirty)). \`make dev-pypi\` goes back."

.PHONY: dev-pypi
dev-pypi: $(TARGET)/.uv-installed ## Go back to the locked, published fred libraries after dev-local
	VIRTUAL_ENV= $(UV_RUN) sync --locked --extra dev
	@touch $(TARGET)/.compiled
	@echo "Dependencies installed from the lock."

.PHONY: update
update: $(TARGET)/.uv-installed ## Re-resolve and update all dependencies
	VIRTUAL_ENV= $(UV_RUN) lock --upgrade
	VIRTUAL_ENV= $(UV_RUN) sync --locked --extra dev
	@touch $(TARGET)/.compiled

# ── The image's two commands ───────────────────────────────────────────────────
# Same shape as the agents pod: $ENV_FILE names the secrets file, which names
# $CONFIG_FILE in turn. A deployed pod mounts a Secret and a ConfigMap and sets
# the same two variables in its Deployment spec.

.PHONY: publish
publish: dev ## Declare this Knowledge Base to Fred (the deployment step)
	@test -f $(ENV_FILE) || { echo "Missing $(ENV_FILE) — copy config/env.template"; exit 1; }
	ENV_FILE=$(ENV_FILE) VIRTUAL_ENV= $(UV_RUN) run python -m $(PY_PACKAGE) publish

.PHONY: run
run: dev ## Serve runs until stopped (needs Temporal and the Control Plane)
	@test -f $(ENV_FILE) || { echo "Missing $(ENV_FILE) — copy config/env.template"; exit 1; }
	ENV_FILE=$(ENV_FILE) VIRTUAL_ENV= $(UV_RUN) run python -m $(PY_PACKAGE) run

# ── A Content Server to try it against ──────────────────────────────────────
# The fake Content Server (fake_content_server/): a local folder served through the same two
# calls, behind the same two identities, with the signature checked the way
# the gateway's reference client computes it. Its credentials are fixed and public:
# account fake-account / fake-password, subscription fake-api-key /
# fake-api-secret, API Manager user fake-apim-user, root folder 2000.

FAKE_CONTENT_SERVER_DIR            ?= $(CURDIR)/fake_content_server/sample
FAKE_CONTENT_SERVER_PORT           ?= 8090
FAKE_CONTENT_SERVER_URL            ?= http://localhost:$(FAKE_CONTENT_SERVER_PORT)/otcs/api/v2
FAKE_CONTENT_SERVER_IMAGE          ?= fake-content-server:latest
FAKE_CONTENT_SERVER_CONTAINER_NAME ?= fake-content-server
FAKE_CONTENT_SERVER_NETWORK        ?= fred-shared-network

.PHONY: fake-content-server-run
fake-content-server-run: dev ## Serve a folder as a fake Content Server: make fake-content-server-run [FAKE_CONTENT_SERVER_DIR=/path]
	FAKE_CONTENT_SERVER_ROOT_DIR=$(FAKE_CONTENT_SERVER_DIR) FAKE_CONTENT_SERVER_PORT=$(FAKE_CONTENT_SERVER_PORT) \
		VIRTUAL_ENV= $(UV_RUN) run python -m fake_content_server

.PHONY: fake-content-server-sync
fake-content-server-sync: dev ## One dry run against the fake started by fake-content-server-run [LIBRARY=...]
	ENV_FILE=$(ENV_FILE) OPENTEXT_KB_PASSWORD=fake-password \
		OPENTEXT_KB_API_KEY=fake-api-key OPENTEXT_KB_API_SECRET=fake-api-secret \
		VIRTUAL_ENV= $(UV_RUN) run $(CLI) sync --folder-id 2000 --account fake-account \
		--base-url $(FAKE_CONTENT_SERVER_URL) --gateway thales-api-manager --api-username fake-apim-user \
		$(if $(INCLUDE),--include '$(INCLUDE)',) $(if $(LIBRARY),--library '$(LIBRARY)',)

.PHONY: fake-content-server-build
fake-content-server-build: ## Build the fake Content Server's image (the k3d stand-in)
	docker build -f dockerfiles/Dockerfile.fake-content-server -t $(FAKE_CONTENT_SERVER_IMAGE) .

.PHONY: fake-content-server-docker-run
fake-content-server-docker-run: fake-content-server-build ## Serve the sample tree from the fake Content Server's image
	-@docker rm -f $(FAKE_CONTENT_SERVER_CONTAINER_NAME) >/dev/null 2>&1 || true
	@docker network inspect $(FAKE_CONTENT_SERVER_NETWORK) >/dev/null 2>&1 \
		|| docker network create $(FAKE_CONTENT_SERVER_NETWORK) >/dev/null
	docker run -d --name $(FAKE_CONTENT_SERVER_CONTAINER_NAME) --network $(FAKE_CONTENT_SERVER_NETWORK) \
		-p $(FAKE_CONTENT_SERVER_PORT):8080 $(FAKE_CONTENT_SERVER_IMAGE)
	@echo "fake Content Server: $(FAKE_CONTENT_SERVER_URL) (from a container: http://$(FAKE_CONTENT_SERVER_CONTAINER_NAME):8080/otcs/api/v2)"

.PHONY: fake-content-server-stop
fake-content-server-stop: ## Stop the fake Content Server's container
	-@docker rm -f $(FAKE_CONTENT_SERVER_CONTAINER_NAME) 2>/dev/null || true

# ── The deployable image ───────────────────────────────────────────────────────
# Build from this repository root using the committed dependency lock.
KB                    ?= opentext
KB_PACKAGE            ?= opentext_kb
# Named after this package and tagged with its
# version: what is built here is what the chart's appVersion names.
VERSION               := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)
# A deployment pushes it to its own registry: DOCKER_REGISTRY=registry.example.com/team
DOCKER_REGISTRY       ?=
DOCKER_IMAGE_NAME     ?= $(if $(DOCKER_REGISTRY),$(DOCKER_REGISTRY)/,)opentext-knowledge-base
DOCKER_IMAGE_TAG      ?= $(VERSION)
DOCKER_IMAGE          ?= $(DOCKER_IMAGE_NAME):$(DOCKER_IMAGE_TAG)
DOCKER_CONTAINER_NAME ?= opentext-kb
DOCKERFILE_PATH       ?= Dockerfile
DOCKER_CONTEXT        ?= .
DOCKER_NETWORK        ?= fred-shared-network
DOCKER_USER_NAME      ?= fred-user
# Fixed, unlike the agents pod's: an image pushed to a registry must not carry
# whichever uid the developer who built it happens to have.
DOCKER_USER_ID        ?= 1000
DOCKER_GROUP_ID       ?= 1000
CONFIG_DIR            ?= $(CURDIR)/config
CONTAINER_CONFIG_DIR  ?= /app/config

.PHONY: docker-build
docker-build: ## Build the deployable image for this Knowledge Base pod (FRED_LIBS=local: fred from FRED_REPO)
ifeq ($(FRED_LIBS),local)
	@$(MAKE) --no-print-directory fred-repo-check
	@echo "fred's libraries from $(FRED_REPO) ($$(git -C $(FRED_REPO) describe --always --dirty))"
else ifneq ($(FRED_LIBS),pypi)
	$(error FRED_LIBS must be pypi or local, not '$(FRED_LIBS)')
endif
	docker build \
		-f $(DOCKERFILE_PATH) \
		--build-arg FRED_LIBS=$(FRED_LIBS) \
		$(if $(filter local,$(FRED_LIBS)),--build-context fred=$(FRED_REPO)) \
		--build-arg KB=$(KB) \
		--build-arg KB_PACKAGE=$(KB_PACKAGE) \
		--build-arg USER_NAME=$(DOCKER_USER_NAME) \
		--build-arg USER_ID=$(DOCKER_USER_ID) \
		--build-arg GROUP_ID=$(DOCKER_GROUP_ID) \
		-t $(DOCKER_IMAGE) \
		$(DOCKER_CONTEXT)

# What to run between building the image and pushing it. With no network at
# all: the SDK's entry point loads this Knowledge Base, the declaration it would
# publish can be projected, the `run` path imports its workflow engine, and no
# secrets file reached the image.
.PHONY: docker-smoke
docker-smoke: ## Check the built image offline before pushing it
	docker run --rm --network none $(DOCKER_IMAGE) --help >/dev/null
	docker run --rm --network none --entrypoint $(CLI) $(DOCKER_IMAGE) declaration >/dev/null
	docker run --rm --network none --entrypoint python $(DOCKER_IMAGE) \
		-c "import $(PY_PACKAGE).document_boundary, temporalio"
	docker run --rm --network none --entrypoint sh $(DOCKER_IMAGE) \
		-c 'test ! -e /app/config/.env || { echo "config/.env is in the image"; exit 1; }'
	@echo "Image $(DOCKER_IMAGE) passed the offline smoke test."

.PHONY: docker-push
docker-push: ## Push the image (authenticate to the selected registry first)
	docker push $(DOCKER_IMAGE)

.PHONY: docker-publish
docker-publish: ## Declare this Knowledge Base to Fred, from the image
	docker run --rm \
		--network $(DOCKER_NETWORK) \
		--add-host=host.docker.internal:host-gateway \
		-v $(realpath $(CONFIG_DIR)):$(CONTAINER_CONFIG_DIR):ro \
		-e ENV_FILE=$(CONTAINER_CONFIG_DIR)/.env \
		$(DOCKER_IMAGE) publish

.PHONY: docker-run
docker-run: ## Serve runs from the image, joined to fred-shared-network
	docker run --rm \
		--name $(DOCKER_CONTAINER_NAME) \
		--network $(DOCKER_NETWORK) \
		--add-host=host.docker.internal:host-gateway \
		-v $(realpath $(CONFIG_DIR)):$(CONTAINER_CONFIG_DIR):ro \
		-e ENV_FILE=$(CONTAINER_CONFIG_DIR)/.env \
		$(DOCKER_IMAGE)

.PHONY: docker-stop
docker-stop: ## Stop the running container by name
	-@docker stop $(DOCKER_CONTAINER_NAME) 2>/dev/null || true

.PHONY: docker-clean
docker-clean: ## Remove this container and image only
	-@docker rm -f $(DOCKER_CONTAINER_NAME) 2>/dev/null || true
	-@docker rmi $(DOCKER_IMAGE) 2>/dev/null || true

# ── Offline developer tool ─────────────────────────────────────────────────────

.PHONY: declaration
declaration: dev ## Print what the image's `publish` command would send to Fred
	VIRTUAL_ENV= $(UV_RUN) run $(CLI) declaration

.PHONY: sync
sync: dev ## Synchronize a Content Server folder once: make sync FOLDER=123 ACCOUNT=me [LIBRARY=... INCLUDE=...]
	@if [ -z "$(FOLDER)" ] || [ -z "$(ACCOUNT)" ]; then \
		echo "Provide a folder and an account: make sync FOLDER=123456789 ACCOUNT=my-account"; \
		exit 1; \
	fi
	ENV_FILE=$(ENV_FILE) VIRTUAL_ENV= $(UV_RUN) run $(CLI) sync $(SYNC_FLAGS)

.PHONY: watch
watch: dev ## Synchronize over and over: make watch FOLDER=... ACCOUNT=... LIBRARY=... [INTERVAL=30]
	@if [ -z "$(FOLDER)" ] || [ -z "$(ACCOUNT)" ]; then \
		echo "Provide a folder and an account: make watch FOLDER=123456789 ACCOUNT=my-account"; \
		exit 1; \
	fi
	ENV_FILE=$(ENV_FILE) VIRTUAL_ENV= $(UV_RUN) run $(CLI) watch $(SYNC_FLAGS) \
		$(if $(INTERVAL),--interval $(INTERVAL),)

# Shared by sync and watch: one list, so the two can never drift apart. The
# password is $OPENTEXT_KB_PASSWORD, never a flag.
SYNC_FLAGS = --folder-id '$(FOLDER)' --account '$(ACCOUNT)' \
	$(if $(BASE_URL),--base-url '$(BASE_URL)',) \
	$(if $(GATEWAY),--gateway '$(GATEWAY)',) \
	$(if $(API_USERNAME),--api-username '$(API_USERNAME)',) \
	$(if $(INCLUDE),--include '$(INCLUDE)',) \
	$(if $(EXCLUDE),--exclude '$(EXCLUDE)',) \
	$(if $(MAX_FILES),--max-files $(MAX_FILES),) \
	$(if $(PROFILE),--profile '$(PROFILE)',) \
	$(if $(TRUST_ANY_CERTIFICATE),--trust-any-certificate,) \
	$(if $(LIBRARY),--library '$(LIBRARY)',)

# ── Code quality ───────────────────────────────────────────────────────────────
# Same tool set and baselines as the sibling packages: ruff (lint + format),
# bandit (SAST), detect-secrets (leak scan), basedpyright (type-check).

.PHONY: lint
lint: dev ## Run the linter (ruff)
	VIRTUAL_ENV= $(UV_RUN) run ruff check

.PHONY: lint-fix
lint-fix: dev ## Run the linter (ruff) and fix what's auto-fixable
	VIRTUAL_ENV= $(UV_RUN) run ruff check --fix

.PHONY: import-order
import-order: dev ## Check import order (ruff)
	VIRTUAL_ENV= $(UV_RUN) run ruff check --select I

.PHONY: import-order-fix
import-order-fix: dev ## Fix import order (ruff)
	VIRTUAL_ENV= $(UV_RUN) run ruff check --select I --fix

.PHONY: format
format: dev ## Check formatting (ruff)
	VIRTUAL_ENV= $(UV_RUN) run ruff format --check

.PHONY: format-fix
format-fix: dev ## Fix formatting (ruff)
	VIRTUAL_ENV= $(UV_RUN) run ruff format

.PHONY: sast
sast: dev ## Run bandit (SAST), gated on .baseline/bandit-baseline.json
	VIRTUAL_ENV= $(UV_RUN) run bandit \
		-r $(PY_PACKAGE) fake_content_server \
		-s $(BANDIT_IGNORED_RULES) \
		--baseline $(BANDIT_BASELINE_FILE)

.PHONY: detect-secret
detect-secret: dev ## Run detect-secrets, gated on .baseline/detect-secret-baseline.json
	git ls-files --cached --others --exclude-standard -z . | xargs -0 env UV_CACHE_DIR=$(UV_CACHE_DIR) $(UV) run detect-secrets-hook \
		--baseline $(DETECT_SECRET_BASELINE_FILE) \
		--exclude-files '$(DETECT_SECRET_EXCLUDE_FILES)'

.PHONY: type-check
type-check: dev ## Run basedpyright, gated on .baseline/basedpyright-baseline.json
	VIRTUAL_ENV= $(UV_RUN) run basedpyright --baseline-file $(BASEDPYRIGHT_BASELINE_FILE)

.PHONY: code-quality
code-quality: dev lint format sast detect-secret type-check ## Run all code quality checks

.PHONY: code-quality-fix
code-quality-fix: dev lint-fix import-order-fix format-fix ## Auto-fix formatting/imports/linting

$(BASELINE_DIR):
	mkdir -p $(BASELINE_DIR)

.PHONY: baseline-sast
baseline-sast: $(BASELINE_DIR) dev ## (Re)generate the bandit baseline
	VIRTUAL_ENV= $(UV_RUN) run bandit \
		-r $(PY_PACKAGE) fake_content_server \
		-s $(BANDIT_IGNORED_RULES) \
		-f json \
		-o $(BANDIT_BASELINE_FILE)

.PHONY: baseline-detect-secret
baseline-detect-secret: $(BASELINE_DIR) dev ## (Re)generate the detect-secrets baseline
	$(UV_RUN) run detect-secrets scan . > $(DETECT_SECRET_BASELINE_FILE)

.PHONY: baseline-type-check
baseline-type-check: $(BASELINE_DIR) dev ## (Re)generate the basedpyright baseline
	VIRTUAL_ENV= $(UV_RUN) run basedpyright --baseline-file $(BASEDPYRIGHT_BASELINE_FILE) --writebaseline

.PHONY: baseline
baseline: format-fix ## Format code and (re)generate all quality baselines
	-$(MAKE) baseline-sast
	-$(MAKE) baseline-detect-secret
	-$(MAKE) baseline-type-check

# ── Tests ──────────────────────────────────────────────────────────────────────

PYTEST_OPTS ?=

.PHONY: test
test: dev ## Run the offline test suite with coverage (no external services)
	@status=0; \
		VIRTUAL_ENV= $(UV_RUN) run pytest $(PYTEST_OPTS) -m "not integration" \
			--disable-socket --allow-unix-socket \
			--cov=. --cov-config=.coveragerc --cov-report=html || status=$$?; \
		if [ "$$status" -eq 5 ]; then \
			echo "No tests collected; treating offline validation as successful."; \
		elif [ "$$status" -ne 0 ]; then \
			exit "$$status"; \
		fi
	@echo "Coverage report: htmlcov/index.html"

.PHONY: test-one
test-one: dev ## Run a specific test: make test-one TEST=path::to::test
	@if [ -z "$(TEST)" ]; then \
		echo "Provide a test path: make test-one TEST=path::to::test"; \
		exit 1; \
	fi
	VIRTUAL_ENV= $(UV_RUN) run pytest $(PYTEST_OPTS) -v $(TEST)

.PHONY: clean
clean: ## Remove the virtual environment and cached files
	rm -rf $(VENV) $(TARGET)
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
