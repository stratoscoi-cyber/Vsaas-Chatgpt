.PHONY: help install test lint run-wfaas run-traas run-lgaas frontend migrate seed compose-up compose-down clean

help:
	@echo "Targets:"
	@echo "  install       install runtime + dev dependencies"
	@echo "  test          run the pytest suite"
	@echo "  run-wfaas     run the weather/agronomy service (:5001)"
	@echo "  run-traas     run the routing service (:5002)"
	@echo "  run-lgaas     run the prisaMove logistics marketplace (:5003)"
	@echo "  frontend      serve the dashboard (:8080)"
	@echo "  migrate       apply Alembic migrations to all three databases"
	@echo "  seed          insert demo data"
	@echo "  compose-up    docker compose up --build"
	@echo "  compose-down  docker compose down -v"

install:
	pip install -r requirements-dev.txt

test:
	pytest -q

run-wfaas:
	python -m agri_platform.wfaas.app

run-traas:
	python -m agri_platform.traas.app

run-lgaas:
	python -m agri_platform.marketplace.app

frontend:
	python -m http.server --directory frontend 8080

migrate:
	alembic -c alembic-wfaas.ini upgrade head
	alembic -c alembic-traas.ini upgrade head
	alembic -c alembic-lgaas.ini upgrade head

seed:
	python manage.py seed

compose-up:
	docker compose up --build

compose-down:
	docker compose down -v

clean:
	rm -f wfaas.db traas.db lgaas.db
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
