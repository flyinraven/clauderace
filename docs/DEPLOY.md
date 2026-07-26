# Deploying exam.txglobal.com.au

Three pieces, in this order. Allow about 45 minutes the first time.

| Piece | Lives on | Why there |
|---|---|---|
| PostgreSQL database | SiteGround | Render's free PostgreSQL **expires after 30 days** and is deleted |
| API (Python) | Render, Singapore | Free tier; Singapore is the closest region to Australia |
| Website (React) | SiteGround | Static files; already paid for |

---

## Step 1 — Create the database on SiteGround

1. Site Tools → **Site** → **PostgreSQL** → **Databases** → **Create Database**.
   Note the name it gives you (something like `dbxxxxxxx_race`).
2. Go to the **Users** tab → **Create User**. Use a long random password —
   this database is reachable from the internet, so the password is the security
   boundary. Copy it somewhere safe now; SiteGround will not show it again.
3. Still on **Users**, grant that user access to the database you just made.
4. Go to the **Remote** tab. This is the step people miss: remote connections are
   blocked by default. Add Render's outbound IP addresses here.

   Find them at: Render Dashboard → your service → **Connect** → **Outbound**.
   There are usually three. Add each one.

   > If you cannot find them yet, create the Render service first (Step 2), then
   > come back. The API cannot reach the database until this is done, and the
   > symptom is a hanging request rather than a clear error.

5. Assemble the connection string:

   ```
   postgresql+psycopg://USER:PASSWORD@HOSTNAME:5432/DATABASE
   ```

   The hostname is shown on the PostgreSQL page. Keep the `+psycopg` part.

---

## Step 2 — Deploy the API to Render

1. Push this repository to GitHub (private).
2. Render Dashboard → **New** → **Blueprint** → select the repository.
   It reads `render.yaml` and creates the service.
3. Open the service → **Environment**, and set the four values marked
   `sync: false`:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | the connection string from Step 1 |
   | `SETTINGS_ENCRYPTION_KEY` | **your existing local key** — see the warning below |
   | `BOOTSTRAP_ADMIN_EMAIL` | your email |
   | `BOOTSTRAP_ADMIN_PASSWORD` | a strong password; used once to create the account |

   > **`SETTINGS_ENCRYPTION_KEY` must match the one in `backend/.env`.**
   > It encrypts your OpenRouter and Google API keys inside the database. If
   > Render generates a different one, every migrated key becomes permanently
   > unreadable and you would have to paste them all in again. Never rotate it.

4. Deploy. The first build takes a few minutes. On boot the app runs its own
   database migrations and creates your administrator account.
5. Check `https://YOUR-SERVICE.onrender.com/api/health` returns
   `{"status":"ok"}`, and `/api/ready` reports `"database":"connected"`.
   If `/api/ready` says unavailable, the IP whitelist in Step 1.4 is wrong.

---

## Step 3 — Migrate the question bank

Do this **after** the API's first successful boot, so the tables exist.

From the repo root, with the backend virtualenv active:

```bash
python scripts/migrate_to_production.py --target "postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME" --dry-run
```

Check the counts look right, then run it again without `--dry-run`.

This carries across the questions, model answers, examiner feedback, figures,
OSCE stations and station images — the material that cost real money to
generate. It refuses to run if the target already has questions, so it cannot
silently duplicate the bank.

Sittings, jobs and logs are deliberately left behind as local test noise.

---

## Step 4 — Publish the website

1. Point `exam.txglobal.com.au` at your SiteGround hosting (Site Tools →
   **Domain** → **Subdomains**, or DNS if the domain is elsewhere), and issue a
   Let's Encrypt certificate under **Security** → **SSL Manager**.
2. Build and upload:

   ```powershell
   .\scripts\deploy_frontend.ps1 -ApiUrl https://YOUR-SERVICE.onrender.com
   ```

   Add `-SshHost`, `-SshUser` and `-SshPort` (from Site Tools → **Devs** → **SSH
   Keys Manager**) to upload automatically. Without them the script just builds,
   and you upload the **contents** of `frontend/dist` through File Manager.

3. Confirm `.htaccess` is present in the document root. Without it, refreshing
   on `/osce` returns a 404 — the single most common symptom of a missed step.

---

## Afterwards

- Sign in at `https://exam.txglobal.com.au` with the bootstrap administrator.
- Go to **Admin → Settings** and press **Test connection**. If the migration
  carried your keys across it will pass immediately; if not, paste them again.
- Issue invite codes under **Admin → Users** for anyone else.

## Things that will bite you

**The API sleeps.** Render's free tier stops the service after 15 minutes idle,
and the next request takes about a minute while it restarts. The app shows a
"waking the server" notice rather than looking broken. Timed exams are safe: the
clock is computed from timestamps on the server, so a cold start cannot cost you
exam time. If you want it always warm, an uptime pinger hitting `/api/health`
every 10 minutes works, or upgrade to a paid instance.

**Free instance hours.** 750 per month per workspace, which is roughly one
service running continuously. A second free service would exhaust it.

**Uploads and images live in the database.** Render's disk is wiped on every
deploy, so PDFs and images are stored as bytes in PostgreSQL. Keep an eye on
your SiteGround disk quota if you ingest a lot of documents.

**Back up before you experiment.** Site Tools → **PostgreSQL** → **Backups**, or
run the migration script in reverse into a local SQLite file.
