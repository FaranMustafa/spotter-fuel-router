# spotter-fuel-router

A Django REST API that takes a start and finish location in the USA, returns the driving
route, and picks cost-optimal fuel stops along it (500-mile range, 10 mpg) using the
supplied fuel price list, together with the total fuel cost. See `docs/SPEC.md` for the
full specification.

## Setup

With [uv](https://docs.astral.sh/uv/) (recommended):

```sh
uv sync
```

With pip (Python 3.12+):

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # or requirements.txt for runtime only
```

## Run

```sh
uv run python manage.py migrate
uv run python manage.py runserver
```

Health check: `GET http://127.0.0.1:8000/api/health/`.

## Test

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
