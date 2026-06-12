# Repository Guidelines
##General Rules
From now on, any code modifications must first have an OpenSpec change (using the openspec-propose skill).
Any change must first answer:
1. Why make the change?
2. What will be changed?
3. What will not be changed?
4. How will it be accepted?
5. How to roll back?
6. Will it affect existing RAG / Agent / data entry / configuration / testing?
If the answers are unclear, writing code is prohibited.

## Project Structure & Module Organization

This repository contains a FastAPI-based RAG knowledge QA service. Application code lives in `app/`: `api/` defines HTTP routes, `services/` contains RAG, embedding, indexing, and document-splitting logic, `tools/` exposes agent tools, `core/` holds Milvus integration, and `models/` contains Pydantic request/response schemas. The browser UI is in `static/`, sample content is in `docs/`, and Milvus/Attu local services are defined in `vector-database.yml`.

## Build, Test, and Development Commands

- `uv venv && .venv\Scripts\activate`: create and activate a Windows virtual environment.
- `uv pip install -e ".[dev]"`: install the package plus development tools.
- `docker compose -f vector-database.yml up -d`: start Milvus, MinIO, etcd, and Attu.
- `python -m uvicorn app.main:app --host 0.0.0.0 --port 9900 --reload`: run the API and static UI locally.
- `pytest`: run the test suite configured in `pyproject.toml`.
- `ruff check app tests`, `black app tests`, `isort app tests`, `mypy app`: lint, format, sort imports, and type-check.

## Coding Style & Naming Conventions

Use Python 3.11+ and keep line length to 100 characters. Formatting is managed by Black and isort; Ruff enforces common errors, import order, bugbear checks, and pyupgrade rules. Prefer explicit, typed functions for service boundaries. Name modules and files with `snake_case.py`; classes use `PascalCase`; functions, variables, and FastAPI route handlers use `snake_case`.

## Testing Guidelines

`pyproject.toml` expects tests under `tests/`, although this directory is not present yet. Add tests as `tests/test_<module>.py`, mirror the `app/` module being covered, and use `pytest-asyncio` for async FastAPI or service tests. Coverage is configured with `--cov=app`; add focused tests for API behavior, document splitting, vector indexing, and RAG tool orchestration when changing those areas.

## Commit & Pull Request Guidelines

This checkout has no Git history, so use clear Conventional Commit-style subjects such as `feat: add upload validation` or `fix: handle empty retrieval results`. Keep commits scoped to one logical change. Pull requests should include a short summary, test results, related issue links, and screenshots or API examples when UI or endpoint behavior changes.

## Security & Configuration Tips

Keep secrets in `.env`; it is ignored by Git. At minimum, set `DASHSCOPE_API_KEY` before running the app. Do not commit generated `uploads/`, `logs/`, `volumes/`, coverage reports, or cache directories. When changing Milvus settings, update both `app/config.py` defaults and local Docker instructions if developer setup changes.
