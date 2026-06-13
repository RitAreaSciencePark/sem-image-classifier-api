SERVICE ?= sem-classifier
ENV ?= dev
PYTHON ?= python3
REPO_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

export PYTHONPATH := $(REPO_ROOT)

.PHONY: render render-prod status render-all validate verify verify-prod prod-pack deploy access token test-service teardown infra-deploy infra-configure new-service check-prereqs fresh fresh-all configure-all deploy-all test-all render-prod-all prod-pack-all bootstrap-dev

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
	$(PYTHON) -m compileall -q src/

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

test-service:
	SERVICE=$(SERVICE) $(PYTHON) $(REPO_ROOT)/tests/test_api.py

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

configure-all:
	@for svc in services/*/service.yaml; do \
		id=$$(basename $$(dirname $$svc)); \
		$(MAKE) infra-configure SERVICE=$$id; \
	done

infra-deploy:
	$(REPO_ROOT)/k8s/infra.sh deploy

infra-configure:
	$(REPO_ROOT)/k8s/infra.sh configure --service $(SERVICE)

bootstrap-dev: check-prereqs infra-deploy
	@echo "Authentik infra deployed. Run: make deploy-all DEPLOY_ARGS=--rebuild && make configure-all"

new-service:
	$(PYTHON) -m ml_platform.generator.scaffold --service $(SERVICE) \
		--model-id "$(MODEL_ID)" --model-source "$(MODEL_SOURCE)" \
		--api-port $(API_PORT) --authentik-port $(AUTHENTIK_PORT)

status:
	$(REPO_ROOT)/k8s/app.sh --service $(SERVICE) status
