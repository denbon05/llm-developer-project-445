.PHONY: help install setup test lint format lock infra migrate seed stand api actions pdp guardrail snapshot smoke scenarios reset

# Группа smoke-проверок: make smoke G=0. Без G прогоняются все группы.
G ?=

# Locally everything runs through uv. Hexlet's checker image has no uv: it
# installs requirements.txt with pip and calls these targets with plain python.
UV := $(shell command -v uv 2>/dev/null)
RUN := $(if $(UV),uv run,)

help:
	@echo "make install    - зависимости (uv sync)"
	@echo "make setup      - зависимости, схема и демо-данные (база должна быть поднята)"
	@echo "make infra      - postgres+pgvector (docker compose)"
	@echo "make migrate    - применить db/*.sql"
	@echo "make seed       - демо-данные + база знаний в Postgres"
	@echo "make stand      - стенд готового: issuer + read-MCP + моки + веб-чат (:8100)"
	@echo "make api        - backend + оркестратор (:8000)         [агенты]"
	@echo "make actions    - action-MCP (:8220)                    [контракты]"
	@echo "make pdp        - PDP с политиками Dogwood (:9100)      [права]"
	@echo "make guardrail  - guardrail-сервис (:8300)              [guardrail]"
	@echo "make snapshot   - пересчитать векторы базы знаний (нужна LM Studio)"
	@echo "make smoke      - проверки по шагам, группа: make smoke G=0"
	@echo "make scenarios  - диалоговые сценарии на живой LLM      [сценарии]"
	@echo "make reset      - вернуть демо-данные моков в исходное"
	@echo "make lint       - ruff + pylint (короткие имена), F=<файлы>"
	@echo "make format     - ruff: автоисправления и форматирование, F=<файлы>"
	@echo "make lock       - обновить uv.lock и requirements.txt"

install:
ifdef UV
	uv sync --frozen
else
	python -m pip install -r requirements.txt
endif

setup: install migrate seed

test: smoke

# Keep requirements.txt in sync with uv.lock: Hexlet's checker installs from it.
lock:
	uv lock
	uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt

F ?= .
lint:
	uv run ruff check $(F)
	uv run ruff format --check $(F)
	uv run pylint $(F)

format:
	uv run ruff check --fix-only $(F)
	uv run ruff format $(F)

infra:
	docker compose -p operator-chat-ai up -d

migrate:
	$(RUN) python -m core.db migrate

seed:
	$(RUN) python -m core.seed

stand:
	$(RUN) uvicorn --factory stand:build_app --host 127.0.0.1 --port 8100 --log-level warning

api:
	$(RUN) uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level warning

actions:
	$(RUN) uvicorn actions.server:app --host 127.0.0.1 --port 8220 --log-level warning

pdp:
	$(RUN) uvicorn --factory pdp.service:build_app --host 127.0.0.1 --port 9100 --log-level warning

guardrail:
	$(RUN) uvicorn --factory guardrail.service:build_app --host 127.0.0.1 --port 8300 --log-level warning

snapshot:
	$(RUN) python -m kb.build_snapshot

smoke:
	$(RUN) python -m tests.smoke $(G)

scenarios:
	$(RUN) python -m scripts.run_scenarios

reset:
	curl -s -X POST http://127.0.0.1:8100/mock/admin/reset
