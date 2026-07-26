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
  this is it.

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
6. SMTP is unconfigured, so invites are copied by hand. Fine for a few users.
7. No backup routine. SiteGround has PostgreSQL backups - worth switching on.
