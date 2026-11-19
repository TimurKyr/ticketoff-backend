# ticketoff-backend

Backend for **ticketoff** — self-service event ticketing platform (academic MVP).

- **Stack:** Python · FastAPI · PostgreSQL · SQLAlchemy 2.0 (sync, psycopg 3) · Alembic · Pydantic v2
- **Role owners:** Lead (auth, events, admin, notifications), Backend #1 (tickets, orders, payments), Backend #2 (promos, analytics, support)
- **API contract:** source of truth is `/openapi.json` — web & mobile clients are generated from it.

## Getting started
```bash
cp .env.example .env
docker compose up --build        # API on http://localhost:8000 (GET /health, /docs)

# Local development
uv sync
docker compose up -d db          # Postgres for tests and local runs
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run alembic upgrade head
```

## Demo
Demo data for the web app, the scanner app and the presentation:

```bash
uv run alembic upgrade head
DEMO_PASSWORD='<password>' uv run python -m app.scripts.seed_demo --print-tokens --qr-dir demo-qr
```

On the server: `docker compose -f docker-compose.prod.yml exec api python -m app.scripts.seed_demo`
(see `docs/DEPLOY.md`). The seed is idempotent: run it again and it creates nothing.

It creates 4 venues (Almaty, Astana), three accounts (`demo.organizer@`, `demo.scanner@`,
`demo.buyer@ticketoff.store`, all with `DEMO_PASSWORD`; without it a random password is printed
once), and three published events titled `Demo: ...`:

| Event | What it shows |
| --- | --- |
| Demo: Открытая лекция | free registration; tickets `free-valid`, `free-checked-in`, `free-cancelled` |
| Demo: Kazakh Symphony Night | paid sales activated (simulated); tickets `paid-valid`, `paid-refunded`, `paid-promo` (bought with `DEMO20`, 20% off) |
| Demo: Almaty Jazz Weekend | several ticket types (free, Standard, VIP); tickets `multi-vip`, `multi-standard` |

`--qr-dir` writes one PNG per ticket plus `promo-DEMO20.png` (the campaign link QR).
`--reset-scans` makes the valid demo tickets valid again after a scanner run.

Demo scenario (the same flow is tested end to end in `tests/test_core_flow.py`):
1. Organizer (`demo.organizer@`) opens the paid event: ticket types, activation checklist, promo
   campaign and the `DEMO20` code with its QR.
2. Buyer (`demo.buyer@`) opens the event (or scans `promo-DEMO20.png`, which links to it with
   the code), applies `DEMO20` at checkout, pays with the simulated card and gets the ticket QR.
3. Scanner (`demo.scanner@`) in the mobile app scans: the new ticket → `checked_in`, again →
   `already_used`; `free-cancelled` → `cancelled`, `paid-refunded` → `refunded`, `multi-vip` at
   another event → `wrong_event`, `promo-DEMO20.png` → `campaign_qr_rejected`.
4. Support case about the order: after BE-17.

## Structure
See `app/` — modular monolith: `core/`, `modules/<domain>/{router,service,repository,models,schemas}.py`, `common/`.

## Conventions
Branching, commits, and PR rules: see CONTRIBUTING.md (WIP).

## Team & workflow

- **Task tracking:** Jira (Scrum, 2-week sprints).
- **Branching & PR rules:** see the shared team git-flow guide (posted in the team chat).
- All changes land on `main` through a **Pull Request** (1 approval, squash merge, linear history).
