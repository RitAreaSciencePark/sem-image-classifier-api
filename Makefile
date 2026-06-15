# =============================================================================
# SEM Classifier API — operator Makefile
# Primary interface: make help
# =============================================================================

SERVICE ?= sem-classifier
ENV ?= dev
PYTHON ?= python3
REPO_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

export PYTHONPATH := $(REPO_ROOT)

.DEFAULT_GOAL := help

.PHONY: help help-advanced render render-prod render-all render-prod-all validate verify verify-prod prod-pack prod-pack-all check-prereqs deploy deploy-all access token test-unit test-e2e test-service test-all teardown fresh fresh-all configure-all infra-deploy infra-configure bootstrap-dev onboard new-service status stress-test usage-report autoscale-validate autoscale-validate-heavy

# -----------------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------------

help:
	@echo "SEM Classifier API — common targets (SERVICE=$(SERVICE))"
	@echo ""
	@echo "  make onboard SERVICE=x MODEL_ID=org/model   New service: scaffold + render + validate + secrets"
	@echo "  make render SERVICE=x                       Generate services/x/generated/dev/"
	@echo "  make deploy SERVICE=x DEPLOY_ARGS=--rebuild Build, push GHCR, deploy to dev cluster"
	@echo "  make fresh SERVICE=x                        Teardown + rebuild + configure"
	@echo "  make access SERVICE=x                       SSH tunnel + port-forward"
	@echo "  make token SERVICE=x                        Get M2M JWT from Authentik"
	@echo "  make test-service SERVICE=x                 E2E tests (reads secrets.local.yaml)"
	@echo "  make verify SERVICE=x                       Validate + unit tests"
	@echo "  make render-prod SERVICE=x                  Generate prod bundle"
	@echo "  make verify-prod SERVICE=x                  Prod preflight (must pass before kubectl)"
	@echo "  make prod-pack SERVICE=x                    Tarball for prod operator"
	@echo ""
	@echo "  make check-prereqs                          Verify podman, kubectl, k8s/.env"
	@echo "  make infra-deploy                           Deploy shared Authentik (dev)"
	@echo "  make bootstrap-dev                          Infra only; then deploy-all + configure-all"
	@echo ""
	@echo "Advanced (multi-service): make help-advanced"

help-advanced:
	@echo "Multi-service targets:"
	@echo "  make render-all          make render-prod-all"
	@echo "  make deploy-all          make fresh-all"
	@echo "  make configure-all       make test-all"
	@echo "  make prod-pack-all"

# -----------------------------------------------------------------------------
# Codegen
# -----------------------------------------------------------------------------

render:
	$(PYTHON) -m ml_platform.generator.render --service $(SERVICE) --env $(ENV)

render-prod:
	$(PYTHON) -m ml_platform.generator.render --service $(SERVICE) --env prod

render-all:
	@for svc in services/*/service.yaml; do \
		id=$$(basename $$(dirname $$svc)); \
		$(MAKE) render SERVICE=$$id ENV=dev; \
	done

render-prod-all:
	@for svc in services/*/service.yaml; do \
		id=$$(basename $$(dirname $$svc)); \
		$(MAKE) render-prod SERVICE=$$id; \
	done

validate:
	$(PYTHON) -m ml_platform.generator.validate --service $(SERVICE)

verify: validate
	$(PYTHON) -m compileall -q src/ scripts/ ml_platform/devtools tests/
	$(PYTHON) -m pytest tests/unit -q
	$(PYTHON) -m pytest tests/e2e --collect-only -q

verify-prod:
	$(PYTHON) -m ml_platform.generator.verify_prod --service $(SERVICE)

prod-pack: render-prod verify-prod
	@tar -czf $(REPO_ROOT)/services/$(SERVICE)/generated/prod-bundle.tar.gz \
		-C $(REPO_ROOT)/services/$(SERVICE)/generated prod
	@sha256sum $(REPO_ROOT)/services/$(SERVICE)/generated/prod-bundle.tar.gz \
		> $(REPO_ROOT)/services/$(SERVICE)/generated/prod-bundle.tar.gz.sha256
	@echo "Packed: services/$(SERVICE)/generated/prod-bundle.tar.gz"

prod-pack-all:
	@for svc in services/*/service.yaml; do \
		id=$$(basename $$(dirname $$svc)); \
		$(MAKE) prod-pack SERVICE=$$id; \
	done

# -----------------------------------------------------------------------------
# New service onboarding
# -----------------------------------------------------------------------------

onboard:
	@test -n "$(MODEL_ID)" || { echo "FAIL: set MODEL_ID=org/model-name"; exit 1; }
	$(PYTHON) -m ml_platform.generator.scaffold onboard \
		--service $(SERVICE) --model-id "$(MODEL_ID)" --model-source "$(MODEL_SOURCE)" \
		--api-port $(or $(API_PORT),8080) --authentik-port $(or $(AUTHENTIK_PORT),9001)

new-service: onboard
	@echo "Note: new-service is an alias for onboard"

# -----------------------------------------------------------------------------
# Dev deploy & cluster ops
# -----------------------------------------------------------------------------

check-prereqs:
	@command -v podman >/dev/null || { echo "FAIL: podman not found"; exit 1; }
	@command -v kubectl >/dev/null || { echo "FAIL: kubectl not found"; exit 1; }
	@command -v ssh >/dev/null || { echo "FAIL: ssh not found"; exit 1; }
	@test -f $(REPO_ROOT)/k8s/.env || { echo "FAIL: k8s/.env missing — copy from k8s/.env.example"; exit 1; }
	@grep -qE '^GHCR_TOKEN=' $(REPO_ROOT)/k8s/.env && ! grep -q 'replace_with' $(REPO_ROOT)/k8s/.env || { echo "FAIL: set GHCR_TOKEN in k8s/.env"; exit 1; }
	@echo "Prerequisites OK"

deploy: render validate
	$(REPO_ROOT)/k8s/app.sh --service $(SERVICE) deploy $(DEPLOY_ARGS)

deploy-all:
	@for svc in services/*/service.yaml; do \
		id=$$(basename $$(dirname $$svc)); \
		$(MAKE) deploy SERVICE=$$id DEPLOY_ARGS="$(DEPLOY_ARGS)"; \
	done

access:
	$(REPO_ROOT)/k8s/app.sh --service $(SERVICE) access

token:
	$(REPO_ROOT)/k8s/app.sh --service $(SERVICE) token

status:
	$(REPO_ROOT)/k8s/app.sh --service $(SERVICE) status

teardown:
	$(REPO_ROOT)/k8s/app.sh --service $(SERVICE) reset --yes

fresh: check-prereqs
	$(REPO_ROOT)/k8s/app.sh --service $(SERVICE) reset --yes
	$(MAKE) deploy SERVICE=$(SERVICE) DEPLOY_ARGS=--rebuild
	$(MAKE) infra-configure SERVICE=$(SERVICE)

fresh-all:
	@for svc in services/*/service.yaml; do \
		id=$$(basename $$(dirname $$svc)); \
		$(MAKE) fresh SERVICE=$$id; \
	done

# -----------------------------------------------------------------------------
# Shared Authentik infra (dev)
# -----------------------------------------------------------------------------

infra-deploy:
	$(REPO_ROOT)/k8s/infra.sh deploy

infra-configure:
	$(REPO_ROOT)/k8s/infra.sh configure --service $(SERVICE)

configure-all:
	@for svc in services/*/service.yaml; do \
		id=$$(basename $$(dirname $$svc)); \
		$(MAKE) infra-configure SERVICE=$$id; \
	done

bootstrap-dev: check-prereqs infra-deploy
	@echo "Authentik infra deployed. Run: make deploy-all DEPLOY_ARGS=--rebuild && make configure-all"

# -----------------------------------------------------------------------------
# Tests & benchmarks
# -----------------------------------------------------------------------------

test-unit:
	$(PYTHON) -m pytest tests/unit -v

test-e2e:
	SERVICE=$(SERVICE) $(PYTHON) -m pytest tests/e2e -v -m e2e

test-service: test-e2e

test-all:
	@for svc in services/*/service.yaml; do \
		id=$$(basename $$(dirname $$svc)); \
		$(REPO_ROOT)/k8s/app.sh --service $$id access; \
	done
	@for svc in services/*/service.yaml; do \
		id=$$(basename $$(dirname $$svc)); \
		echo "=== Testing $$id ==="; \
		$(MAKE) test-service SERVICE=$$id || exit 1; \
	done

stress-test:
	SERVICE=$(SERVICE) $(PYTHON) $(REPO_ROOT)/scripts/load/stress_test.py --service $(SERVICE)

usage-report:
	@ns="$(NAMESPACE)"; \
	if [ -z "$$ns" ]; then ns=$$(grep '^NAMESPACE=' $(REPO_ROOT)/services/$(SERVICE)/generated/dev/deploy.env | cut -d= -f2 | tr -d '"'); fi; \
	test -n "$$ns" || { echo "FAIL: set NAMESPACE= or run make render SERVICE=$(SERVICE)"; exit 1; }; \
	test -x $(REPO_ROOT)/services/$(SERVICE)/generated/dev/usage-report/run.sh || { echo "FAIL: run make render SERVICE=$(SERVICE)"; exit 1; }; \
	$(REPO_ROOT)/services/$(SERVICE)/generated/dev/usage-report/run.sh \
		--namespace $$ns --since 24h --format html --output-dir /tmp

autoscale-validate:
	SERVICE=$(SERVICE) $(PYTHON) $(REPO_ROOT)/scripts/benchmarks/autoscale_validate.py --service $(SERVICE) --profile light

autoscale-validate-heavy:
	SERVICE=$(SERVICE) $(PYTHON) $(REPO_ROOT)/scripts/benchmarks/autoscale_validate.py --service $(SERVICE) --profile heavy --phase $(or $(PHASE),heavy-run) $(if $(OUTPUT_DIR),--output-dir $(OUTPUT_DIR),)
