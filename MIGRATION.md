# Moving the API off Render

The database is on SiteGround and the front end is on SiteGround. Only the
FastAPI service moves, and it holds no state, so there is nothing to migrate
except configuration.

## Why

Render's free instance is 512 MB and killed the service repeatedly on 10 August
2026 - twice out of memory during an ingest, three times because the health
check could not answer within five seconds while a batch job held the core.
From the outside that reads as a server that will not wake up. Render's fix is
the 2 GB tier at US$25/month; Railway gives the same headroom for less.

## The service

| | |
|--|--|
| Root directory | `backend` |
| Builder | Nixpacks (reads `backend/.python-version`, currently 3.14) |
| Start command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health check | `/api/health` |

`backend/railway.json` carries the last three, so the only thing to set by hand
is the root directory.

## Environment

Copy these from Render's dashboard. Four are secrets and must be pasted by
hand:

| variable | notes |
|--|--|
| `DATABASE_URL` | secret - the SiteGround Postgres string, contains the password |
| `SECRET_KEY` | secret - signs login tokens; changing it logs everyone out |
| `SETTINGS_ENCRYPTION_KEY` | secret - **must be identical**. It decrypts the AI keys held in the settings table, and a different value makes every one of them permanently unreadable |
| `BOOTSTRAP_ADMIN_PASSWORD` | secret |
| `BOOTSTRAP_ADMIN_EMAIL` | not secret |
| `ENVIRONMENT` | `production` |
| `DEBUG` | `false` |
| `CORS_ORIGINS` | `https://exam.txglobal.com.au,https://www.exam.txglobal.com.au` |

## After it is up

1. Check `https://<new-host>/api/health` returns `{"status":"ok"}`.
2. Rebuild the front end against the new host and upload it:

   ```
   .\scripts\deploy_frontend.ps1 -ApiUrl https://<new-host> `
       -SshHost ssh.txglobal.com.au -SshUser u2166-agq370poby4y `
       -SshPort 18765 -IdentityFile $env:USERPROFILE\.ssh\race_siteground
   ```

   The API URL is baked into the bundle at build time, so this step is what
   actually moves the site over.
3. Sit a station end to end - record, transcribe, grade. Transcription and
   grading are the two things a candidate waits at the screen for, and they run
   through the job worker rather than the request.
4. Leave Render running until that passes. Nothing points at it once the front
   end is rebuilt, so it costs nothing to keep as a fallback.

## Watch for

- **Python 3.14.** Nixpacks must offer it. If the build cannot find it, pin a
  version it does have in `backend/.python-version` and check the dependencies
  in `requirements.txt` still resolve.
- **Usage billing.** Render killed a runaway batch at 512 MB; Railway will run
  it and invoice. Set a spend limit before queueing bulk work.
- **The job worker is a thread inside the web process.** With more memory that
  stops mattering, but it is still the reason a long batch and a candidate
  compete. `BUSY_YIELD_SECONDS` in `app/services/jobs/runner.py` is what keeps
  the site answering while a batch runs.
