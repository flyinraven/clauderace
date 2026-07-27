# RANZCO RACE Exam Simulator

Private mock-exam and assessment platform for RANZCO Advanced Clinical Examination
(RACE) candidates. Deployed at `exam.txglobal.com.au`.

## What it does

- **Ingests past papers.** Upload a RANZCO Examiners' Report (PDF/DOCX/TXT/JSON) and it
  extracts every question, sub-question, mark allocation, curriculum standard, clinical
  figure and per-examiner feedback comment.
- **Writes model answers.** The reports publish the questions and what candidates got
  wrong, but never the answers. Model answers are generated per sub-question as discrete
  marked key points, conditioned on that question's examiner feedback.
- **Generates new questions** across the nine subspecialties (in progress).
- **Runs timed mock exams** replicating the real RACE clock (in progress).
- **Grades automatically** with two independent examiner passes and an Angoff cut score
  (in progress).

## Stack

| Layer | Technology | Hosting |
|---|---|---|
| Frontend | React 19 + TypeScript + Vite + Tailwind 4 | SiteGround (static) |
| Backend | Python 3.14 + FastAPI + SQLAlchemy 2 | Render (free web service) |
| Database | PostgreSQL | SiteGround |
| AI | OpenRouter by default, swappable in the admin portal | — |

## Running locally

**Backend** (from `backend/`):

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill it in, then:

```bash
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

The schema is migrated automatically on startup, and the bootstrap administrator is
created from `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`.

**Frontend** (from `frontend/`):

```bash
npm install && npm run dev
```

Open http://localhost:5173. API requests proxy to port 8000 in development.

## Layout

```
backend/app/
  api/          FastAPI routers (auth, admin, documents, questions, jobs)
  models/       SQLAlchemy tables
  services/
    ai/         provider-agnostic model gateway
    ingest/     extract -> segment -> structure -> persist
    answers/    model answer generation
    jobs/       chunked, resumable background job runner
    marking.py  rules shared by written and OSCE marking
    coerce.py   forgiving conversion of model output
frontend/src/
  pages/        screens, admin screens under pages/admin
  components/   shared UI primitives
```

## Operational notes

- **Render's free tier sleeps after 15 minutes idle** (~1 min cold start). Long tasks run
  as chunked jobs that persist a cursor after every step, so a sleep never loses work.
  Frontend job polling doubles as a keep-alive.
- **Render's free PostgreSQL expires after 30 days**, which is why the database lives on
  SiteGround instead.
- **Render has no persistent disk**, so uploaded PDFs and images are stored in the
  database and served through the API with immutable cache headers.
- `SETTINGS_ENCRYPTION_KEY` must be set in production. API keys in the `settings` table
  are Fernet-encrypted with it; losing it makes every stored key unreadable.

## Copyright

RANZCO examiners' reports are copyright and may not be reproduced. This platform is a
private, invitation-only study tool — do not make it publicly accessible without RANZCO's
written permission.
