# uv is installed under ~/.local/bin; fall back to `python3 -m uv` if it is not on PATH.
UV := $(shell command -v uv 2>/dev/null || echo "python3 -m uv")

.PHONY: sync test lint fmt bench run-mock-llm run-mock-downstream run-task1 run-task2 run-task3 run-task4 clean

sync:
	$(UV) sync

test: sync
	$(UV) run pytest -q

lint: sync
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy .

fmt:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

run-mock-llm:
	$(UV) run uvicorn mock_provider.llm:app --port 8100

run-mock-downstream:
	$(UV) run uvicorn mock_provider.mcp_downstream:app --port 8200

run-task1:
	$(UV) run python -m task1_mcp_server

run-task2:
	$(UV) run uvicorn task2_mcp_gateway.app:app --port 8300

run-task3:
	$(UV) run uvicorn task3_stream_guardrail.app:app --port 8400

run-task4:
	$(UV) run uvicorn task4_router.app:app --port 8500

bench:
	$(UV) run python -m task3_stream_guardrail.benchmark

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache **/__pycache__ *.db *.db-wal *.db-shm
