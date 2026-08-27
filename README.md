# ticketoff-backend

Backend for **ticketoff** — self-service event ticketing platform (academic MVP).

- **Stack:** Python · FastAPI · PostgreSQL · SQLAlchemy 2.0 (async) · Alembic · Pydantic v2
- **Role owners:** Lead (auth, events, admin, notifications), Backend #1 (tickets, orders, payments), Backend #2 (promos, analytics, support)
- **API contract:** source of truth is `/openapi.json` — web & mobile clients are generated from it.

## Getting started
```bash
# TODO (Lead, week 1): uv sync && docker compose up
```

## Structure
See `app/` — modular monolith: `core/`, `modules/<domain>/{router,service,repository,models,schemas}.py`, `common/`.

## Conventions
Branching, commits, and PR rules: see CONTRIBUTING.md (WIP).

## Team & workflow

- **Task tracking:** Jira (Scrum, 2-week sprints).
- **Branching & PR rules:** see the shared team git-flow guide (posted in the team chat).
- All changes land on `main` through a **Pull Request** (1 approval, squash merge, linear history).
