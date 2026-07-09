# [FORCA] Command Grid — operator command surface.
# Thin, discoverable wrappers over docker compose + the scripts/ helpers.
# Production targets use docker-compose.prod.yml; dev targets use docker-compose.yml.
#
# Quick start:  make setup   (creates .env)  →  edit .env  →  make deploy  →  make bootstrap  →  make health
.DEFAULT_GOAL := help

# docker compose (v2) or legacy docker-compose, whichever exists.
DC   := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")
PROD := docker-compose.prod.yml
DEV  := docker-compose.yml

.PHONY: help setup build deploy update migrate collectstatic bootstrap bootstrap-sample \
        import-sde import-assets prices health logs ps down restart shell dbshell \
        backup restore create-admin dev dev-down dev-logs cert config-check \
        lint lint-fix test test-fast check audit sbom rollback

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[1;36m%-18s\033[0m %s\n", $$1, $$2}'

# --- first-run ---------------------------------------------------------------
setup: ## Create .env from the template if it does not exist
	@if [ -f .env ]; then echo ".env already exists — leaving it untouched."; \
	else cp .env.example .env && echo "Created .env from .env.example. Edit it, then run 'make deploy'."; fi

# --- production lifecycle ----------------------------------------------------
build: ## Build the production images
	$(DC) -f $(PROD) build

deploy: ## Build + start the prod stack, then migrate & collectstatic
	$(DC) -f $(PROD) up -d --build
	@bash scripts/wait-for-services.sh
	$(DC) -f $(PROD) exec -T web python manage.py migrate --noinput
	$(DC) -f $(PROD) exec -T web python manage.py collectstatic --noinput
	@echo "Deploy complete. Next: 'make bootstrap' (first install) then 'make health'."

rollback: ## Roll back to an earlier revision (REF=v1.0.0 [DUMP=./backups/....sql.gz])
	@bash scripts/rollback.sh $(REF) $(if $(DUMP),--restore $(DUMP),)

update: ## Pull latest code, rebuild, migrate (safe upgrade path)
	@bash scripts/update.sh

migrate: ## Apply database migrations
	$(DC) -f $(PROD) exec -T web python manage.py migrate --noinput

collectstatic: ## Collect static assets
	$(DC) -f $(PROD) exec -T web python manage.py collectstatic --noinput

# --- data & assets -----------------------------------------------------------
bootstrap: ## Load EVE reference data (full SDE + PI + referenced images)
	@bash scripts/bootstrap-data.sh

bootstrap-sample: ## Load the tiny bundled sample SDE (dev/CI only)
	@bash scripts/bootstrap-data.sh --sample --no-images

import-sde: ## Import the full Static Data Export from Fuzzwork
	$(DC) -f $(PROD) exec -T web python manage.py import_sde_fuzzwork

import-assets: ## Mirror referenced EVE type images locally
	$(DC) -f $(PROD) exec -T web python manage.py mirror_type_images --referenced-only

prices: ## Price referenced types from Jita (first pass)
	$(DC) -f $(PROD) exec -T web python manage.py price_types

create-admin: ## Ensure a Django superuser (EMAIL=you@example.com)
	@bash scripts/create-admin.sh "$(EMAIL)"

# --- operations --------------------------------------------------------------
health: ## Run the full health check
	@bash scripts/healthcheck.sh

logs: ## Tail logs for all prod services (Ctrl-C to stop)
	$(DC) -f $(PROD) logs -f --tail=100

ps: ## Show prod container status
	$(DC) -f $(PROD) ps

restart: ## Restart the prod stack
	$(DC) -f $(PROD) restart

down: ## Stop the prod stack (data volumes preserved)
	$(DC) -f $(PROD) down

shell: ## Open a Django shell in the web container
	$(DC) -f $(PROD) exec web python manage.py shell

dbshell: ## Open a psql shell
	$(DC) -f $(PROD) exec postgres sh -c 'psql -U "$$POSTGRES_USER" "$$POSTGRES_DB"'

backup: ## Dump the database to ./backups
	@bash scripts/backup.sh

restore: ## Restore the DB from a dump (FILE=./backups/forca-....sql.gz)
	@bash scripts/restore.sh "$(FILE)"

cert: ## Obtain/renew TLS cert (DOMAIN=... EMAIL=...); run with sudo
	sudo bash scripts/cert-init.sh "$(DOMAIN)" "$(EMAIL)"

config-check: ## Validate the compose files parse
	$(DC) -f $(PROD) config -q && echo "prod compose OK"
	$(DC) -f $(DEV)  config -q && echo "dev compose OK"

# --- local development -------------------------------------------------------
dev: ## Start the dev stack (runserver + autoreload)
	$(DC) -f $(DEV) up -d --build
	@echo "Dev app: http://127.0.0.1:8000  —  run 'make bootstrap-sample' for seed data."

dev-down: ## Stop the dev stack
	$(DC) -f $(DEV) down

dev-logs: ## Tail dev logs
	$(DC) -f $(DEV) logs -f --tail=100

# --- quality gates -----------------------------------------------------------
# These mirror .github/workflows/ci.yml so a contributor can reproduce CI locally.
# They run inside the dev stack's web container, which already has requirements-dev.
#
# --ds is passed explicitly: the dev compose file exports
# DJANGO_SETTINGS_MODULE=config.settings.dev, and that environment variable takes
# precedence over pyproject.toml's [tool.pytest.ini_options] value. Without --ds the
# suite would run against dev settings and fail trying to reach Redis.
TEST_DS := --ds=config.settings.test

lint: ## Run the ruff linter (same checks as CI)
	$(DC) -f $(DEV) run --rm --no-deps web ruff check .

lint-fix: ## Auto-fix what ruff can fix
	$(DC) -f $(DEV) run --rm --no-deps web ruff check . --fix

test: ## Run the full pytest suite (needs the dev stack's postgres)
	$(DC) -f $(DEV) run --rm web pytest -q $(TEST_DS)

test-fast: ## Run pytest, stopping at the first failure
	$(DC) -f $(DEV) run --rm web pytest -q -x $(TEST_DS)

check: ## Django system checks (add --deploy for prod-hardening warnings)
	$(DC) -f $(DEV) run --rm --no-deps web python manage.py check

audit: ## Scan runtime dependencies for known vulnerabilities
	$(DC) -f $(DEV) run --rm --no-deps web pip-audit -r requirements.txt --progress-spinner off

sbom: ## Write a CycloneDX SBOM of the runtime dependencies to ./sbom.cdx.json
	@$(DC) -f $(DEV) run --rm --no-deps -T web \
	  pip-audit -r requirements.txt -f cyclonedx-json --progress-spinner off 2>/dev/null > sbom.cdx.json
	@echo "Wrote sbom.cdx.json"
