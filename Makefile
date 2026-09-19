# Shortcuts. Everything here also works as a plain command; see the README.

VENV := .venv/bin

.PHONY: help demo test lint fmt sim replay clean

help:
	@echo "make demo     run the service with a demo kitchen loaded, and open it"
	@echo "make test     run the test suite"
	@echo "make lint     ruff check and format --check"
	@echo "make fmt      apply ruff formatting"
	@echo "make sim      print the three decision scenarios, no server"
	@echo "make replay   re-record snapshots and rebuild the published walkthrough"
	@echo "make clean    remove the database, caches and build output"

demo:
	./demo.sh

test:
	$(VENV)/python -m pytest

lint:
	$(VENV)/ruff check .
	$(VENV)/ruff format --check .

fmt:
	$(VENV)/ruff format .
	$(VENV)/ruff check --fix .

sim:
	$(VENV)/python -m app.sim.scenario

replay:
	$(VENV)/python -m app.sim.capture
	$(VENV)/python -m tools.build_replay

clean:
	rm -f managers.db managers.db-wal managers.db-shm
	rm -rf .pytest_cache .ruff_cache dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
