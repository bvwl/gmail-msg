.PHONY: format lint typecheck test check

format:
	conda run -n zy314 python -m ruff format app tests

lint:
	conda run -n zy314 python -m ruff check app tests

typecheck:
	conda run -n zy314 python -m mypy app tests

test:
	conda run -n zy314 python -m pytest

check: lint typecheck test

