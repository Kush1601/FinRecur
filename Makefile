.PHONY: dev db api web seed test e2e eval lint

db:
	docker compose up -d db

api:
	cd backend && uv run uvicorn api.main:app --reload --port 8000

web:
	cd web && npm run dev

dev: db
	@echo "run 'make api' and 'make web' in two terminals"

seed: db
	cd backend && uv run alembic upgrade head && uv run python ../scripts/seed.py

test:
	cd backend && uv run pytest -q
	cd web && npm test --silent

e2e: seed
	cd web && E2E=1 npx playwright test

eval: seed
	cd backend && uv run python ../scripts/eval.py

lint:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run pyright
	cd web && npm run lint && npx tsc --noEmit
