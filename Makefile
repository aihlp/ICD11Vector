# Makefile for antigravity

.PHONY: install dev validate test lint typecheck clean link link-check fetch-icd11 fetch-icd11-limit

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

validate:
	python scripts/validate.py

test:
	pytest

lint:
	ruff check .

typecheck:
	mypy scripts

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +

link:
	python scripts/link_symptoms.py

link-check:
	python scripts/link_symptoms.py --check

fetch-icd11:
	python scripts/fetch_icd11.py

fetch-icd11-limit:
	python scripts/fetch_icd11.py --limit 10
