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
- 60 OSCE stations, all with examiner prompts and split findings
- 57 stations show a live image (46 faithful, 11 representative)
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

## Next up

**Restructure the station presentation so it gives nothing away.** Currently a
station shows a full patient history, which hands the candidate the diagnosis.
It should show only:

1. Demographics — child / adult / elderly, male / female
2. The opening instruction — "examine the anterior segment", "assess the squint"
3. The image

Everything else is elicited. This needs a new field (or a rewrite of
`patient_history`) across all 60 stations, a change to the station generator
prompt, and a change to what `GET /osce/sittings/{id}` returns.
