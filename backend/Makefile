# Project 300K — backend. Loads .env (if present) for run/test targets.
ifneq (,$(wildcard ./.env))
include .env
export
endif

DEV_DB ?= project300k_dev
TEST_DB ?= project300k_test

.PHONY: run build vet test test-int createdb dropdb createtestdb tidy \
        up down logs ps psql ts-status ts-ip grafana-logs grafana-url

run: ## Run the server (applies migrations on startup)
	go run ./cmd/server

build: ## Compile the server binary to bin/server
	go build -o bin/server ./cmd/server

vet:
	go vet ./...

tidy:
	go mod tidy

test: ## Unit tests only (no database needed)
	go test ./...

# Integration tests run only when TEST_DATABASE_URL is set; this target wires it
# to a throwaway local Postgres database, recreated each run.
test-int: createtestdb ## Unit + integration tests against a fresh local test DB
	TEST_DATABASE_URL="postgres://localhost:5432/$(TEST_DB)?sslmode=disable" go test ./...

createdb: ## Create the local dev database
	createdb $(DEV_DB) || true

dropdb:
	dropdb --if-exists $(DEV_DB)

createtestdb:
	dropdb --if-exists $(TEST_DB)
	createdb $(TEST_DB)

# --- docker compose (db + api as two containers) ---------------------------
up: ## Build and start the full stack (db + api) in the background
	docker compose up -d --build

down: ## Stop the stack (data in the pgdata volume is preserved)
	docker compose down

logs: ## Follow the api container logs
	docker compose logs -f api

ps:
	docker compose ps

psql: ## Open a psql shell inside the db container
	docker compose exec db psql -U app -d project300k

ts-status: ## Show the Tailscale sidecar status (node + tailnet peers)
	docker compose exec tailscale tailscale status

ts-ip: ## Print this laptop's tailnet IP (point the Pi's API_URL/TAILSCALE_IP here)
	docker compose exec tailscale tailscale ip -4

grafana-logs: ## Follow the grafana container logs
	docker compose logs -f grafana

grafana-url: ## Print the Grafana URLs (local + tailnet)
	@echo "local:  http://127.0.0.1:3000"
	@echo "tailnet: http://$$(docker compose exec -T tailscale tailscale ip -4 | head -n1):3000"
