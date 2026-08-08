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

Passing `-SshHost`/`-SshUser`/`-SshPort`/`-IdentityFile` makes the script do
the upload too, including the `chmod 755` that scp otherwise leaves at 700 -
which serves a blank site. The SSH values are in `backend/.env.production`
and the key is `~/.ssh/race_siteground`.

**Check the deployed bundle, not the script's exit code.** A build made before
pulling deploys cleanly and silently ships the previous version. Fetch
`index.html`, read the `assets/index-*.js` name out of it, and confirm it
matches what `vite build` just printed - on 8 Aug a deploy reported success
while the live bundle was three commits behind and had none of the fixes in
it.

## Content in production

- 36 SEQs + 63 VSAQs, 250 sub-questions, **every one has a marking key**
- 822 marking key points, 65 examiner feedback records
- 4 published papers (Papers 1–4)
- **219 OSCE stations**, all with examiner prompts: 60 generated and 159
  ingested from ten past OSCE reports (2021 Sem 1 through 2025 Sem 1)
- 214 of 219 stations show a live image; 789 figures in all, every one with a
  recorded modality and a caption written without the station in view. 364 come
  from the examiners' own reports. Every station has
  its findings split into given (acuity, pressure) and elicited
- Circuits are built on demand, one station per subspecialty, never repeating a
  station this candidate has sat

## The exam this is for

**The user sits the RANZCO OSCE on 3-4 September 2026.** Judge everything
against that date: only two things count before it, that a station can be sat
end to end without a defect interrupting it, and that there is enough
non-repeating content to sit one most days. Sitting is nearly free - a marked
nine-station circuit costs about three cents - while generating a station costs
USD 0.09 and ingesting one costs USD 0.003. Content is not the constraint.

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

Total spend to date: USD 12.62, of which 5.64 in August 2026.

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
- **Rejecting more images is the wrong lever.** Holding them for approval once
  left stations showing nothing at all. Every fix since moves the *question* to
  match the image, or writes an honest caption beside it - never discards a
  picture or raises a threshold. A station with a loose image and a note beats
  a station with a gap.

## Tests

`cd backend && .venv/Scripts/python -m pytest` - 483 tests, about 90 seconds.

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
- **A background job and a live station compete for one tenth of a CPU.** The
  job worker runs inside the web process on the free tier, so an ingest or an
  image sweep can starve the request handler for a minute at a time. That is
  what cost sitting 42 an answer. The retry survives it; the contention is
  still there. **Do not start an admin job while sitting a station.**
- **Audio is 16 kHz mono WAV, about 32 KB per second** - roughly 3 MB for a
  100-second answer. That is minimal for PCM and cannot be tuned down without
  hurting recognition of clinical terms. Reaching mp3, a tenth the size, needs
  an encoder library in the browser; WAV is not the cause of any failure so far
  and was chosen to keep transcription off Google's 20-a-day free tier.
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

## The 7-8 August pass over question and image quality

A real circuit was sat and scored badly for reasons that were not the
candidate. Each of these is fixed, with the repair applied to the bank:

- **A failed transcription was marked 0%.** Twenty-nine answers were lost to
  the Google AI Studio free tier (20/day) on 3 Aug and five stations published
  at 0%, reading as five subspecialties the candidate was hopeless at. The
  routing cause was already fixed by sending every recording as WAV; the
  marking now refuses a verdict instead, as a partly-marked written paper does.
- **Questions promised images that never arrived.** 60 questions rewritten
  across two passes: a question naming more than is on screen is trimmed to
  what came, and one with nothing on screen has the examiner state the result
  or asks what the candidate would expect. Runs automatically at the end of
  every sourcing batch - `services/osce/reconcile.py`.
- **The stem gave away the diagnosis.** 24 stations, including one that opened
  with "The patient presents with bilateral Brown's Syndrome" beside the visual
  acuity. The findings split is no longer shown the case summary, and a
  deterministic check moves any sentence naming the diagnosis out of GIVEN.
- **The diagnosis was revealed before any differential was asked.** 56
  stations. Stating it mid-station is what the real handouts do, but only after
  the candidate has been made to reason across possibilities.
- **The vision gate agreed with what it was told to expect.** It captioned a
  montage of unilateral Brown's as "bilateral" at confidence 1.00, restating
  the station's own findings. `describe_blind` now looks once with the station
  withheld, and its caption is the one stored - captions are load-bearing, as
  reconciliation matches questions to images by what they say. All 789 figures
  were re-captioned and `modality` is now populated on all of them.

- **147 questions carried no marks.** The builder was told the marks must
  total 20 and that was checked; nothing said every question must be worth
  some, so the model concentrated them. One station had three of its six worth
  nothing, and the marker answers "This question carries no marks" to a reply
  that cost a minute of a nine-minute station. `_unmarked_questions` is now
  part of the arc check, so a new station cannot ship one.
- **A lost answer was marked as one never given.** Sitting 42 on 8 Aug: an
  ingest started eight minutes earlier kept taking the instance down, answer
  B's transcription waited 79 seconds, and answer C never arrived. It scored 0
  of 2.5 - "Nothing was recorded for this question" - and the result read 55%,
  pass, nothing ungraded. The upload now retries three times and keeps the
  recording either way, so the review screen offers to send it again; and a
  failure the client reports withholds the question from marking, as a failed
  transcription does. **Marking still scores a genuinely skipped question
  zero** - the distinction is entirely the client's report.
- **The review showed no images.** A station whose question turned on a picture
  was reviewed without it. The result now returns what the sitting showed, by
  the same `visible_figure` rule rather than a second copy of it - an attached
  and approved image, or the approved words the examiner states where no search
  could find one. A rejected figure is returned by neither.

- **The papers' own images were hidden, and substitutes bought over them.** 116
  figures from the examiners' reports sat marked `not_clinical` - the blind pass
  describes 104 as real clinical images, 55 of them OCTs - while 28 stations
  held both a hidden paper image and a purchased one. `verify_ingested_figures`
  had already stopped gating a paper's own images; these were survivors from
  before that, and the existing figure recheck set all 116 live. Two more gaps
  closed with it: `opening_image_is_settled` did not count `from_paper`, so a
  re-source went shopping over the report's own photograph on 25 stations; and
  the binder could only ever run inside that recheck, so once every figure was
  verified it could not be reached at all. It has its own endpoint now, and
  found two more matches.
- **A question restated for want of an image kept saying so after one arrived.**
  Station 201 was shown its topography beside "her corneal topography shows
  approximately 2 dioptres of regular astigmatism. Talk me through what it
  shows" - the picture and the answer together. A restatement is now undone the
  moment anything binds, and what the restored wording over-promises is trimmed
  in the same pass.

**Three cautions learned the hard way in that pass.** The first two are the same
mistake.

The blind sweep's modality arm
compared an observed modality against `expected_modalities_for`, which for a
figure that named no view guesses from the station's findings blob - "corneal
neovascularisation" yields angiogram and calls a correct slit lamp photograph
wrong. It produced 109 false disagreements before being restricted to questions
that really asked for a named investigation. If a check compares against
something inferred rather than something requested, it is not a check.

And `remark.py` first asked the model to write the marking points AND allocate
the marks so the station still totalled exactly 20. It refused 84 of 98
stations. Hitting an exact sum across six questions is arithmetic, and a
language model is the least reliable way to do it - the marks are worked out in
code now and the model writes wording only.

Both are the same error: giving a model work that should have been computed.
The guard caught each, which is the argument for writing the guard first. But
the re-mark refusals were returned in a dict whose non-integer values the job
tally dropped, so 84 stations declined for reasons nobody could read. **A guard
that refuses silently is only half a guard.**

The third is about the record rather than the rule. Station 201 took three
attempts, each failing differently - restore only on a full match when the case
was partial; restore *or* trim when it needed both; and finally a marker reading
"trim" because the previous attempt had already run, with `reconciled.original`
overwritten by that attempt's own input. The true wording was gone and the
station had to be repaired by hand. Two other repairs deleted what a later one
needed: reconciliation removed `image_wanted` to mean "stop searching", which
also stopped the binder ever matching that question, and six questions lost
their request entirely the same way. **A repair that rewrites its own input
destroys what the next repair needs.** Say the second thing in a second field -
`image_search_exhausted` - and keep the first original, not the latest.

## Repairs that run from an endpoint, not a button

Each was written for a one-off sweep and left without UI. All are admin-only
POSTs, and all are safe to run again - they select only what still needs them.

| Endpoint | What it does |
|---|---|
| `/api/osce/stations/reconcile-questions` | matches questions to the images that arrived |
| `/api/osce/figures/recaption` | describes each image again with the station withheld |
| `/api/osce/stations/remark` | gives marks to questions carrying none |
| `/api/osce/stations/bind-figures` | gives a question the report's own investigation; no model calls |
| `/api/osce/stations/recheck-figures` | puts a report's figures live and records what they are |

Deploy the backend and the front end in that order when a release spans both,
and wait for the new route before shipping the client - otherwise the new
client calls an endpoint that is not there yet. A backend push restarts Render,
which fails any upload in flight, so do not deploy while a station is being
sat.

`scripts/` holds three more that need no model and no key, and repair stored
data directly: `withhold_leaked_diagnoses.py`,
`ask_differentials_before_reveal.py`, `undo_false_modality_downgrades.py`,
`restore_image_requests.py`. Each takes `--apply` and reports without it.

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
2. **132 questions across 84 stations still carry no marks**, 138 minutes of
   clock across the bank. Every station still totals exactly 20, so nothing is
   broken - those questions simply cannot score. `POST
   /api/osce/stations/remark` fixes them and is deployed and tested; the user
   chose to move on rather than run it on 8 Aug 2026. 14 stations were repaired
   by the first, worse version, which let the model set the marks; they total
   20 and have no dead questions, but their allocation was not computed the way
   a re-run would do it.
3. **37 figures genuinely do not match what their question asked for.** The
   questions were made honest about it; the images were not replaced.
4. **The re-caption sweep has no button.** `POST /api/osce/figures/recaption`
   exists and has been run once, by hand, over all 789 figures. Whoever needs
   it next either adds the button beside "Match questions to images" in
   `admin/StationImages.tsx`, or calls the endpoint with an admin token.
5. **Daily circuit** builder works but has never been run through a real
   nine-station sitting.
6. **Written papers** have been sat once end-to-end; the OSCE has been sat for
   a single station. Neither has had a full user run.
7. **Sit one past sitting (deferred - raise once the OSCE work is done).**
   Asked for on 26 Jul 2026 and parked to conserve AI credits for the OSCE.
   Wanted: pick "2026 Semester 1" and sit Papers 1-4 built from that exam's
   own 18 SEQs in the 5/4/5/4 split, topped up with generated VSAQs and
   labelled plainly so it is obvious which parts are authentic.
   Needs an `exam_period` filter on `assemble_paper()` plus the matching
   dropdown in the Question bank, which the API already returns options for
   but the UI never rendered. The reports contain no VSAQs, so real ones for
   a given sitting will never exist.
8. **The rest of the question arc.** The handouts ask for differentials
   grouped by category - hereditary, compressive, inflammatory, infective -
   where the bank asks for a number, and they supply concrete results ("all
   bloods normal and MRI showed...") where the bank poses a hypothetical. The
   marking keys hold flat lists, so this is more than a stem rewrite.
9. **Some questions name the diagnosis in their own stem** - station 261 asks
   for "the diagnostic criteria for Neurofibromatosis Type 1" before any
   differential is possible. A handful of stations; not yet counted.
10. No backup routine. SiteGround has PostgreSQL backups - worth switching on.
   Parked on 8 Aug 2026 at the user's request: `scripts/backup_db.ps1` works
   and has been run by hand, it simply is not scheduled.
