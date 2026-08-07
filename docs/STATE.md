# Project state

Written for picking the work back up in a fresh session. Everything below is
deployed and live unless marked otherwise.

## Where it runs

| Piece | Where |
|---|---|
| Site | `https://exam.txglobal.com.au` — SiteGround, static |
| API | `https://clauderace.onrender.com` — Render, Singapore, free tier |
| Database | SiteGround PostgreSQL |
| Repo | `github.com/flyinraven/clauderace`, branch `main`, auto-deploys the API on push |
| Secrets | `backend/.env.production` (git-ignored) — DB URL, Render API key, SSH details |
| Deploy key | `~/.ssh/race_siteground`, imported into SiteGround as `cluade-deploy` |

Front end is **not** auto-deployed. To ship it:

```powershell
.\scripts\deploy_frontend.ps1 -ApiUrl https://clauderace.onrender.com
```

then `scp` the contents of `frontend/dist` (including `.htaccess`) to
`~/www/exam.txglobal.com.au/public_html`.

## Content in production

- 36 SEQs + 63 VSAQs, 250 sub-questions, **every one has a marking key**
- 822 marking key points, 65 examiner feedback records
- 4 published papers (Papers 1–4)
- 78 OSCE stations, all with examiner prompts: 24 generated and 54 ingested
  from three past OSCE reports (2025 Sem 1, 2025 Sem 2, 2026 Sem 1)
- 75 of 78 stations show a live image, all web-sourced and vision-checked.
  Every station has its findings split into given (acuity, pressure) and
  elicited
- 6 full 9-station circuits with no repeats

## AI routing

Sonnet-5 only where clinical content is invented or judged; everything else on
Gemini Flash at roughly a tenth the cost.

| Task | Model |
|---|---|
| `model_answer`, `generation` | `anthropic/claude-sonnet-5` |
| `structuring`, `utility`, `grading`, `vision` | `google/gemini-2.5-flash` |

All through **OpenRouter**. The Google AI Studio key is configured but unused:
its free tier is **20 requests per day**, which one OSCE circuit exhausts twice
over. Do not route anything to it unless billing is enabled on that project.

Total spend to date: roughly $7.

## Decisions worth not re-litigating

- **Hash routing** (`/#/osce`). SiteGround's nginx answers any non-file path
  with 403 before Apache sees it; disabling NGINX Direct Delivery and flushing
  the cache did not change it. Responses to those paths carry
  `x-proxy-cache-info: DT:1` and no `x-httpd-modphp`.
- **Images auto-approve.** Vision verification already discards diagrams, wrong
  modalities and marketing images. Holding the rest back for approval meant
  stations showed nothing at all. Rejecting one records its URL and searches
  again, skipping everything already turned down.
- **One examiner pass by default** (`grading.examiner_passes`). Two reproduces
  the real double marking and reveals disagreement, at double the cost.
- **Database on SiteGround, not Render.** Render's free PostgreSQL expires
  after 30 days.
- **Email goes out through `resend`, not `smtp`.** Configured and working in
  production. Do not switch the provider back: Render's free tier blocks
  outbound traffic to ports 25/465/587, so the SiteGround mailbox times out
  from there however correct the credentials are. SMTP still works locally.
  Invites now send themselves, and the AI budget warning reaches the
  administrators rather than only the error log. If an invite email arrives
  carrying a bare code with no link, `app.public_url` is unset.
- **No synthetic clinical images.** AI-generated fundus photos look convincing
  and are anatomically wrong.
- **Station question design comes from real examiner handouts.** The RANZCO
  reports record aims, findings, diagnosis and pass requirements but never the
  questions, so the arc in `osce/prompts.py` was taken from mock-exam handouts
  the user holds outside this system: standing instruction, ancillary test
  before the image, differentials by number, the examiner then stating the
  diagnosis so later marks do not depend on earlier ones, management, an
  evolving hypothetical, then straight knowledge. Those handouts are reference
  only - the user does not want that content imported, and it carries patient
  names, dates of birth and MRNs.
- **The OSCE reports carry no clinical photographs.** The 2025 Semester 1 deck
  is 115 pages holding four distinct images: a slide background on 94 pages,
  two more pieces of furniture on 19 each, and a banner on the cover and back.
  Station pages have no images and no vector drawings. Ingestion attaches a
  report's own figures where they exist, but every station image in production
  is web-sourced and vision-checked. Do not re-ingest hoping for images.

## Tests

`cd backend && .venv/Scripts/python -m pytest` - 429 tests, about a minute.

The API is tested end to end against an in-memory database and a fake provider
that sits at `AIClient._post`, so routing, retries, JSON repair, usage
accounting and the budget all run for real. Jobs are drained a chunk at a time
by the `run_jobs` fixture rather than by the worker thread, which keeps them
deterministic while running the identical claim/chunk/resume path.

| File | Covers |
|---|---|
| `test_api_auth.py` | sign-in, invites, and every admin-only route |
| `test_api_osce.py` | a station from browsing to a marked result, and what must not leak while sitting |
| `test_api_exams.py` | the bank, assembly, the clock gate phase by phase, marking |
| `test_station_pipeline.py` | prompt building, findings split, image sourcing and verification |
| `test_api_admin.py` | settings and secret masking, users, documents, jobs, stats |
| `test_ai_client.py` | the gateway, including the vision image size cap |
| `test_query_counts.py` | pages whose query count must not grow with content |
| `test_marking_rules.py` | the rules both marking flows now share |
| `test_job_queue_order.py` | which job runs next, reclaim attempts, and cancellation |
| `test_clock.py`, `test_circuit_repeats.py`, `test_transcription_guard.py` | as before |

Two tests in `test_api_exams.py` skip when Paper 1's spec has no reading phase.
That is deliberate - they assert on a phase that spec may not have.

## Recent changes (most recent last)

- Marking-key token budget scales with question size; a flat 8000 truncated a
  six-part SEQ's JSON. All 250 sub-questions now have keys.
- OSCE station generator added. 60 stations, 6 per subspecialty, 6 full
  circuits. Dedupe compares distinctive words, not exact titles. Generation
  claims its step before working, so a dropped connection loses a station
  rather than duplicating one.
- Images auto-approve and can be rejected with an automatic replacement search
  that skips everything already turned down.
- `grading.examiner_passes` defaults to 1.
- Stations show only `patient_demographic` ("An elderly woman"), the first
  examiner prompt, and the image. Case summary and history are revealed with
  the result.
- Questions are read aloud using the browser's own speech synthesis - free, no
  API. iOS needs `speech.unlock()` called synchronously inside the tap before
  any await, or WebKit drops the utterance silently.
- Ingested stations get their demographic line from the structuring pass, and
  the ingest queues the prompt build itself. Both used to be run by hand after
  every upload, and a station without them is unusable.
- The sitting header no longer names the title or subspecialty; "Oculoplastics
  & Orbit" narrowed the differential before the candidate had looked. Revealed
  with the result.
- Repeating a question pauses the mic, so the synthesised examiner voice is no
  longer recorded and transcribed as the candidate's own answer.
- `deploy_frontend.ps1` chmods after upload: scp creates `assets/` as 700,
  which Apache cannot traverse, and the site loads blank.
- **Images bound to 1568px before any vision call.** Every `ImagePart` shrinks
  itself on construction, so no vision caller can forget. A 2600px web
  photograph went from 1.9 MB to 370 KB, and base64 makes that a 2 MB saving
  per candidate verified - of which there are up to eighteen per station. The
  cap is what providers downsample to anyway, so the tokens charged are
  unchanged. An image already inside the cap is left alone unless re-encoding
  also shrinks it; one over the cap is always shrunk even if that costs bytes,
  because vision is billed by pixel area, not file size. Anything Pillow cannot
  read is sent as it came in.
- **Query counts no longer grow with content.** A marked paper's result page
  cost 77 queries for 30 sub-questions and now costs 10; the question bank page
  is flat at 5 whether it shows 2 questions or 22; opening a 20-question paper
  is 9. Listing circuits no longer reads results one sitting at a time.
  `test_query_counts.py` compares a small page against a large one so a
  reintroduced per-row query fails the suite rather than only production.
- **The station list no longer sends candidates the case summary.** It was the
  fallback display name for a station with no title, so scrolling the list read
  the case out before you chose to sit it. Admins still get it - the station
  images screen needs it to tell one station from another.
- The image endpoint answers `If-None-Match` with a 304. It always advertised
  an ETag but re-read the blob from the database and sent it every time.
- Starting a station and saving a corrected transcript both report failure now.
  Beginning is irreversible, so a silent failure left the screen on "Before you
  begin" while the server counted down; a silently failed correction meant the
  transcript that got marked was not the one on screen. A failed correction also
  stops submission - marking a transcript the candidate has just fixed is worse
  than not marking yet.
- **The marking rules are written once, in `services/marking.py`.** Two passes
  at different temperatures, clamping an award to what the point is worth,
  flagging examiner disagreement, refusing a verdict on a partly-marked result,
  the grade-row upsert and the rounding-drift absorption were all duplicated
  between `grading.grade` and `osce.circuit`, and `circuit` reached into
  `grade` for a private `_examiner_passes` to share the last one. What stays
  per-flow is what genuinely differs: the prompt, the breakdown row shape, the
  cut score (a paper's is set per paper and scaled when only part of it could be
  marked; a station's comes from its own Angoff expectation) and the wording of
  the candidate-facing feedback. `services/coerce.py` holds the model-output
  coercion that had six copies. Checked by recomputing a real marked paper: all
  nine result fields, including the subspecialty breakdown and the feedback
  prose, came back identical to what the old code had stored.
- Transcription no longer primes the model with expected clinical content,
  which was making it fabricate whole answers from quiet audio. Backstops:
  tiny clips are never sent, and transcripts above 3.5 words/second are
  flagged before marking.

## Known issues

- **SiteGround PostgreSQL drops long-lived connections.** Seen twice during
  25-minute batch jobs. Station generation claims its step before doing the
  work so a drop costs one station rather than duplicating one; other job types
  are at-least-once.
- **Three stations have no image**: Fuchs with DMEK plus untreated fellow eye,
  homonymous hemianopia (field charts are almost always annotated, which the
  gate rejects), thyroid restrictive strabismus.
- **iOS `audio/mp4` transcription** — the last sub-question was not transcribed
  in the first real test; the upload was racing the review screen. Fixed but
  not yet re-tested on a phone.
- Three images once came back not auto-approved despite the setting being on.
  Corrected by hand; cause not established. If images appear as "not showing",
  this is it. No defect was found in the auto-approve path when it was reviewed
  on 27 Jul 2026 - `_attach` sets `is_approved` from the setting, and the
  setting reads True from its spec default with no stored row.
- **`backend/race.db` (local development) predates two changes and is
  misleading to test against.** Its 36 stations were ingested on 25 Jul, before
  demographics came from the structuring pass and before images auto-approved,
  so locally every station has no demographic and no visible image. Nothing is
  wrong with the code - production has 75 of 78 stations showing an image. Do
  not diagnose either as a live bug from the local database; re-run the ingest
  locally, or check production.

## Recent changes, continued

- **The job runner no longer pays for work nobody is waiting for.** A reclaim
  spends an attempt, so a chunk that kills the process fails after three rather
  than looping forever. Cancelling is no longer undone by the chunk in flight -
  the worker was writing PENDING from a `job` it had loaded before the
  cancellation committed, so a 28-station batch carried on unless the click
  landed between chunks. `ctx.cancelled` lets a handler stop mid-chunk; image
  sourcing checks it at its phase boundaries, which is where the spend is.
- **A token dies when the password does.** Tokens carry `tv`, the value of
  `users.token_version` they were issued under. Bumped on a password change and
  when an account is disabled. `is_active` was already checked per request, so
  disabling was already immediate; a password change was not.
- **`/auth/login` and `/auth/redeem-invite` are rate limited** - ten failures
  per address per fifteen minutes, ten redemptions per hour, in-process. A
  success clears the count. Sign-in counts against the email as well, because
  `X-Forwarded-For` is forgeable. If this is ever run on more than one
  instance, the counters have to move out of the process.
- **The AI budget warns at 75% before refusing at 100%**, once per calendar
  month, in the error log and by email to the administrators. Threshold in
  Settings (`ai.budget_warn_fraction`, 0 to disable).
- **`station_images.py` (1759 lines) and `api/osce.py` (1381) are now
  packages.** Pure moves: every definition unparses identically and no comment
  was lost. `station_images` splits into constants/queries/verify/describe/
  sourcing/ingested/settle/jobs with a one-way dependency order; `api/osce`
  into helpers/stations/circuits/sittings, and the 33-operation route table was
  compared before and after. Patch a name where it is looked up -
  `station_images.sourcing.build_provider`, not the package.

## Working effectively in a new session

Read this file first, then work from the code. Avoid re-running the expensive
discovery: the PDFs are already ingested, the bank is already built, and
`git log` carries the reasoning for every decision.

Keep tool output small. Long tracebacks and full-file dumps are what make a
session expensive - filter to the lines that matter.

## Next up

Nothing is blocking. Open items, roughly in order of value:

1. **Re-test a station on the phone** — read-aloud, and whether the last
   answer's transcript now lands. Both were fixed but only the first has been
   confirmed by the user.
3. **Daily circuit** builder works but has never been run through a real
   nine-station sitting.
4. **Written papers** have been sat once end-to-end; the OSCE has been sat for
   a single station. Neither has had a full user run.
5. **Sit one past sitting (deferred - raise once the OSCE work is done).**
   Asked for on 26 Jul 2026 and parked to conserve AI credits for the OSCE.
   Wanted: pick "2026 Semester 1" and sit Papers 1-4 built from that exam's
   own 18 SEQs in the 5/4/5/4 split, topped up with generated VSAQs and
   labelled plainly so it is obvious which parts are authentic.
   Needs an `exam_period` filter on `assemble_paper()` plus the matching
   dropdown in the Question bank, which the API already returns options for
   but the UI never rendered. The reports contain no VSAQs, so real ones for
   a given sitting will never exist.
6. No backup routine. SiteGround has PostgreSQL backups - worth switching on.
