SHELL := /bin/bash

.PHONY: help backend frontend compose-up kind-up doctor

help:
	@echo "Available targets: doctor backend frontend compose-up kind-up"

doctor:
	@python --version || true
	@node --version || true
	@docker version || true
	@kubectl version --client || true
	@kind version || true
	@helm version || true

backend:
	cd backend && python -m uvicorn app.main:app --reload --port 8000

frontend:
	cd frontend && npm install && npm run dev

compose-up:
	docker compose up -d

kind-up:
	kind create cluster --config infra/kind/kind-config.yaml
