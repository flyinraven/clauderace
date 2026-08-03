import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { useImage } from '../hooks/useImage'
import { useRecorder } from '../hooks/useRecorder'
import { useSpeech } from '../hooks/useSpeech'
import { formatDuration } from '../hooks/useExamClock'
import { Alert, Badge, Button, Card, Loading, Textarea, cx } from '../components/ui'

interface OscePrompt {
  label: string
  index: number
  text: string
  seconds: number
  marks: number
  // The investigations this question asks you to read. Held back until the
  // question is reached: on screen from the start they would answer themselves.
  // Usually one, but "the OCT and the angiogram" is two, and no single image
  // is both.
  figures: StationFigure[]
  transcript: string | null
  transcript_edited: string | null
  transcription_status: string
  transcription_error: string | null
}

interface StationFigure {
  id: number
  image_id: number | null
  caption: string | null
  // What the examiner states aloud when no photograph of this view exists.
  // Some signs are dynamic - fatiguable ptosis, Cogan's lid twitch - and no
  // still image can carry them at all.
  described_findings: string | null
  position: number
}

interface CircuitNext {
  circuit_id: number
  position: number
  stations: number
  next_station_id: number | null
  rest_seconds: number
  finished: boolean
}

interface Sitting {
  id: number
  station: {
    id: number
    subspecialty: string | null
    title: string | null
    // All you get before you start: "An elderly woman". The case summary and
    // full history are withheld until the result - they name the diagnosis.
    patient_demographic: string | null
    // Only what an examiner would state aloud. The signs you must elicit are
    // withheld until the result.
    findings_given: string | null
    findings_pending_split: boolean
    figures: StationFigure[]
    total_marks: number
  }
  clock: {
    phase: string
    seconds_remaining: number
    can_record: boolean
    station_seconds: number
  }
  current_prompt_index: number
  is_timed: boolean
  submitted_at: string | null
  grading_status: string
  prompts: OscePrompt[]
}

const CLOCK_SYNC_MS = 10_000

/** The station's clinical image — this is the "patient" you are examining. */
function StationFigureView({ figure }: { figure: StationFigure }) {
  const { url } = useImage(figure.image_id)
  const [zoomed, setZoomed] = useState(false)

  // What the examiner states aloud: either the whole view, when no image was
  // found at all, or just the signs the image on screen cannot show. Both are
  // marks the candidate is about to be asked for.
  const spoken = figure.described_findings ? (
    <div className="border-t border-slate-200 bg-slate-50 px-3 py-2">
      <p className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
        On examination
      </p>
      <p className="mt-0.5 text-sm text-slate-700">{figure.described_findings}</p>
    </div>
  ) : null

  // Nothing was found for this view. Rendering the image frame would sit on
  // "Loading image…" for ever.
  if (!figure.image_id) {
    return <div className="rounded-lg border border-slate-200">{spoken}</div>
  }

  return (
    <figure className="overflow-hidden rounded-lg border border-slate-200">
      {url ? (
        <button type="button" className="block w-full cursor-zoom-in" onClick={() => setZoomed(true)}>
          <img src={url} alt={figure.caption ?? 'Clinical image'} className="w-full" />
        </button>
      ) : (
        <div className="flex h-44 items-center justify-center bg-slate-50 text-xs text-slate-400">
          Loading image…
        </div>
      )}
      {figure.caption && (
        <figcaption className="border-t border-slate-200 px-3 py-1.5 text-xs text-slate-600">
          {figure.caption}
        </figcaption>
      )}
      {spoken}
      {zoomed && url && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/85 p-4"
          onClick={() => setZoomed(false)}
          role="presentation"
        >
          <img src={url} alt="" className="max-h-full max-w-full rounded-lg" />
        </div>
      )}
    </figure>
  )
}

export default function OsceStation() {
  const { id } = useParams<{ id: string }>()
  const sittingId = id ? Number(id) : null
  const navigate = useNavigate()

  const [sitting, setSitting] = useState<Sitting | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [index, setIndex] = useState(0)
  const [remaining, setRemaining] = useState(0)
  const [uploading, setUploading] = useState<string | null>(null)
  const [stage, setStage] = useState<'sitting' | 'review'>('sitting')
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [saveFailed, setSaveFailed] = useState<string[]>([])
  const [submitting, setSubmitting] = useState(false)

  const rec = useRecorder()
  const speech = useSpeech()
  const autoAdvanced = useRef(false)

  const load = useCallback(async () => {
    if (!sittingId) return
    try {
      const data = await api<Sitting>(`/osce/sittings/${sittingId}`)
      setSitting(data)
      setRemaining(data.clock.seconds_remaining)
      if (data.submitted_at) setStage('review')
      setIndex((i) => Math.max(i, data.current_prompt_index))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load this station')
    }
  }, [sittingId])

  useEffect(() => {
    void load()
  }, [load])

  // The server owns the clock; this only keeps the display honest between syncs.
  useEffect(() => {
    if (!sitting || sitting.clock.phase !== 'running' || stage !== 'sitting') return
    const tick = window.setInterval(() => setRemaining((v) => Math.max(0, v - 1)), 1000)
    const sync = window.setInterval(async () => {
      try {
        const clock = await api<Sitting['clock']>(`/osce/sittings/${sittingId}/clock`)
        setRemaining(clock.seconds_remaining)
        if (clock.phase === 'expired') {
          setSitting((s) => (s ? { ...s, clock } : s))
        }
      } catch {
        /* keep counting locally */
      }
    }, CLOCK_SYNC_MS)
    return () => {
      window.clearInterval(tick)
      window.clearInterval(sync)
    }
  }, [sitting?.clock.phase, stage, sittingId])

  const prompts = sitting?.prompts ?? []
  const prompt = prompts[index]

  const uploadAnswer = useCallback(
    async (label: string, promptIndex: number, blob: Blob, mimeType: string, durationMs: number) => {
      if (!sittingId) return
      const form = new FormData()
      // Filename extension matters to some servers; derive it from the type.
      const ext = mimeType.includes('wav')
        ? 'wav'
        : mimeType.includes('webm')
          ? 'webm'
          : 'm4a'
      form.append('audio', blob, `answer-${label}.${ext}`)
      form.append('prompt_label', label)
      form.append('prompt_index', String(promptIndex))
      form.append('duration_ms', String(durationMs))

      setUploading(label)
      try {
        await api(`/osce/sittings/${sittingId}/answers`, { method: 'POST', body: form })
      } catch (err) {
        setError(
          err instanceof Error
            ? `Answer ${label} did not upload: ${err.message}`
            : `Answer ${label} did not upload`,
        )
      } finally {
        setUploading(null)
      }
    },
    [sittingId],
  )

  /**
   * Read the current question again. The mic is live by this point, so it is
   * paused for the duration: otherwise the synthesised voice is recorded and
   * transcribed as though the candidate had said it.
   */
  const repeatPrompt = useCallback(async () => {
    if (!prompt) return
    rec.pause()
    try {
      await speech.speak(prompt.text)
    } finally {
      rec.resume()
    }
  }, [prompt, rec, speech])

  /** Finish this answer and move on; the upload runs in the background. */
  const nextPrompt = useCallback(async () => {
    if (!prompt) return
    // Same reason as in begin(): stopping the recorder is async, and iOS stops
    // treating this as a gesture once we await.
    speech.unlock()
    const captured = await rec.stop()
    const isLast = index + 1 >= prompts.length

    if (captured) {
      const upload = uploadAnswer(
        prompt.label,
        prompt.index,
        captured.blob,
        captured.mimeType,
        captured.durationMs,
      )
      // Mid-station the upload overlaps the next answer, which is the point.
      // On the LAST answer it must be awaited: otherwise loading the review
      // screen races the upload and the final response has not been created
      // yet, so it silently shows no transcript.
      if (isLast) await upload
    }

    if (!isLast) {
      const next = prompts[index + 1]
      setIndex(index + 1)
      // Ask the question first, then start recording. Speaking while the mic
      // is live would put the examiner's voice into the candidate's answer.
      // This runs inside the button's gesture handler, which iOS requires.
      if (next) await speech.speak(next.text)
      // Belt and braces: whatever the engine is still doing, stop it before
      // the microphone opens. A question read into the candidate's own answer
      // is worse than a question they have to press Read aloud for.
      speech.cancel()
      await rec.start()
    } else {
      rec.release()
      await load()
      setStage('review')
    }
  }, [prompt, rec, speech, index, prompts, uploadAnswer, load])

  // Time is up: capture whatever was being said and go to review.
  useEffect(() => {
    if (remaining > 0 || stage !== 'sitting' || !sitting?.is_timed || autoAdvanced.current) return
    if (!sitting.clock || sitting.clock.phase === 'not_started') return
    autoAdvanced.current = true
    void (async () => {
      const captured = await rec.stop()
      if (captured && prompt) {
        await uploadAnswer(prompt.label, prompt.index, captured.blob, captured.mimeType, captured.durationMs)
      }
      rec.release()
      await load()
      setStage('review')
    })()
  }, [remaining, stage, sitting, rec, prompt, uploadAnswer, load])

  // Transcription runs in the background, so the review screen polls until
  // every answer has come back. Without this the last one - queued moments
  // before the screen opened - stays blank until manually refreshed.
  useEffect(() => {
    if (stage !== 'review' || !sitting) return
    const pending = sitting.prompts.some((p) => p.transcription_status === 'pending')
    if (!pending) return
    const timer = window.setInterval(() => void load(), 4000)
    return () => window.clearInterval(timer)
  }, [stage, sitting, load])

  const begin = async () => {
    if (!sittingId) return
    // Must happen before any await, while the tap still counts as a gesture.
    speech.unlock()
    const ok = await rec.requestAccess()
    if (!ok) return
    // Starting the clock is irreversible, so a failure here has to be visible:
    // unreported, the screen sat on "Before you begin" with a station that had
    // already started counting down on the server.
    try {
      await api(`/osce/sittings/${sittingId}/begin`, { method: 'POST' })
      const data = await api<Sitting>(`/osce/sittings/${sittingId}`)
      setSitting(data)
      setRemaining(data.clock.seconds_remaining)
      const first = data.prompts[0]
      if (first) await speech.speak(first.text)
      speech.cancel()
      await rec.start()
    } catch (err) {
      rec.release()
      setError(
        err instanceof Error
          ? `The station could not be started: ${err.message}`
          : 'The station could not be started.',
      )
    }
  }

  /** Persist a corrected transcript. This is exactly what gets marked, so a
   *  failed save must never pass silently. */
  const saveEdit = async (label: string) => {
    if (!sittingId) return
    try {
      await api(`/osce/sittings/${sittingId}/answers/${label}/transcript`, {
        method: 'PUT',
        body: { transcript: edits[label] ?? '' },
      })
      setSaveFailed((prev) => (prev.includes(label) ? prev.filter((l) => l !== label) : prev))
    } catch (err) {
      setSaveFailed((prev) => (prev.includes(label) ? prev : [...prev, label]))
      setError(
        err instanceof Error
          ? `Your correction to answer ${label} was not saved: ${err.message}`
          : `Your correction to answer ${label} was not saved.`,
      )
      throw err
    }
  }

  const submit = async () => {
    if (!sittingId) return
    setSubmitting(true)
    try {
      // Corrections go first and a failure stops the submission: marking a
      // transcript the candidate has just fixed is worse than not marking yet.
      for (const label of Object.keys(edits)) await saveEdit(label)
      const outcome = await api<{ circuit: CircuitNext | null }>(
        `/osce/sittings/${sittingId}/submit`,
        { method: 'POST' },
      )
      // A circuit carries straight on: rest, then the next station. The result
      // is held until every station has been sat, which is what the day does -
      // marking runs behind the candidate, not in front of them.
      const circuit = outcome?.circuit
      if (circuit?.next_station_id) {
        navigate(
          `/osce/circuits/${circuit.circuit_id}/rest?next=${circuit.next_station_id}` +
            `&rest=${circuit.rest_seconds}&position=${circuit.position}` +
            `&stations=${circuit.stations}`,
        )
      } else if (circuit?.finished) {
        navigate(`/osce/circuits/${circuit.circuit_id}/result`)
      } else {
        navigate(`/osce/sittings/${sittingId}/result`)
      }
    } catch (err) {
      // saveEdit has already said which answer failed and why.
      setError((prev) => prev ?? (err instanceof Error ? err.message : 'Submission failed'))
      setSubmitting(false)
    }
  }

  if (error && !sitting) return <Alert tone="error">{error}</Alert>
  if (!sitting) return <Loading label="Opening the station…" />

  if (!rec.supported) {
    return (
      <Alert tone="error" title="This browser cannot record audio">
        The OSCE needs microphone recording. On iPhone use Safari (iOS 14.5 or later);
        on desktop use Chrome, Edge or Safari.
      </Alert>
    )
  }

  const notStarted = sitting.clock.phase === 'not_started' && stage === 'sitting'
  const urgent = remaining <= 60 && sitting.is_timed && stage === 'sitting'

  return (
    <div className="space-y-4">
      <div className="sticky top-0 z-20 -mx-4 border-b border-slate-200 bg-white/95 px-4 py-3 backdrop-blur sm:-mx-6 sm:px-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            {/* Title and subspecialty both give the game away - "Oculoplastics
                & Orbit" narrows the differential before the candidate has
                looked at the patient. Held back until the result, like the
                case summary and history. */}
            <p className="text-sm font-semibold text-slate-900">
              {stage === 'review'
                ? sitting.station.title ?? sitting.station.subspecialty ?? 'OSCE station'
                : 'OSCE station'}
            </p>
            <p className="text-xs text-slate-500">
              {stage === 'review' && sitting.station.subspecialty
                ? `${sitting.station.subspecialty} · `
                : ''}
              {sitting.station.total_marks} marks
              {sitting.is_timed ? ' · 9 minutes' : ' · untimed'}
            </p>
          </div>
          {stage === 'sitting' && !notStarted && sitting.is_timed && (
            <p
              className={cx(
                'font-mono text-2xl font-semibold tabular-nums',
                urgent ? 'text-red-600' : 'text-slate-900',
              )}
            >
              {formatDuration(remaining)}
            </p>
          )}
          {stage === 'review' && <Badge tone="amber">Review your answers</Badge>}
        </div>
      </div>

      {error && <Alert tone="error">{error}</Alert>}
      {rec.error && <Alert tone="error">{rec.error}</Alert>}

      {notStarted && (
        <Card title="Before you begin">
          <div className="space-y-3 text-sm text-slate-600">
            <p>
              This station lasts <strong>9 minutes</strong>. You will be asked{' '}
              {prompts.length} questions in turn. Speak your answer aloud, then press
              <strong> Next question</strong> — your answer uploads in the background
              while you read the next one.
            </p>
            <p>
              There is no live transcript (iPhone browsers cannot do that), so watch the
              level meter to confirm the microphone is hearing you. After the station you
              get a chance to correct any mis-heard words before marking.
            </p>
            <p className="text-slate-500">
              Your browser will ask for microphone permission once, now, so it never
              interrupts you mid-station.
            </p>
          </div>

          {speech.supported && (
            <label className="mt-4 flex items-start gap-3 rounded-lg border border-slate-200 p-3">
              <input
                type="checkbox"
                checked={speech.enabled}
                onChange={(e) => speech.setEnabled(e.target.checked)}
                className="mt-1 h-4 w-4 rounded border-slate-300 text-clinical-600 focus:ring-clinical-500"
              />
              <span className="text-sm">
                <span className="font-medium text-slate-800">Read the questions aloud</span>
                <span className="mt-0.5 block text-xs text-slate-500">
                  As the examiner would. Each question is spoken first, then recording
                  starts — so your answer never picks up the examiner's voice.
                </span>
                <span className="mt-1 block text-xs text-slate-500">
                  On iPhone, take the side switch off silent — it mutes web audio.
                  Tap below to check you can hear it.
                </span>
                <button
                  type="button"
                  className="mt-1.5 text-xs font-medium text-clinical-700 underline"
                  onClick={(e) => {
                    e.preventDefault()
                    speech.unlock()
                    void speech.speak('Sound check. You should be able to hear this.')
                  }}
                >
                  🔊 Test the sound
                </button>
              </span>
            </label>
          )}

          <div className="mt-4">
            <Button onClick={begin}>Allow microphone &amp; start</Button>
          </div>
        </Card>
      )}

      {!notStarted && stage === 'sitting' && prompt && (
        <>
          <Card title="The patient">
            <p className="text-lg text-slate-900">
              {sitting.station.patient_demographic ?? 'A patient is seated in front of you.'}
            </p>
            {sitting.station.findings_given && (
              <div className="mt-3">
                <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                  The examiner tells you
                </p>
                <p className="prose-clinical mt-1">{sitting.station.findings_given}</p>
              </div>
            )}

            {sitting.station.figures.length > 0 && (
              <div className="mt-4 grid gap-3 sm:grid-cols-2">
                {sitting.station.figures.map((figure) => (
                  <StationFigureView key={figure.id} figure={figure} />
                ))}
              </div>
            )}

            <p className="mt-3 text-xs text-slate-500">
              Everything else is for you to elicit — the history and clinical signs are
              deliberately withheld until your result, as they would be with a real patient.
            </p>
          </Card>

          {/* "Question C of 5" mixed two countings and read as nonsense: the
              label is the examiner's letter, the position is a number. One
              scheme only - which question this is, out of how many. */}
          <Card
            title={`Question ${index + 1} of ${prompts.length}`}
            actions={<Badge tone="slate">{prompt.marks} marks</Badge>}
          >
            <div className="flex items-start gap-3">
              <p className="flex-1 text-lg font-medium text-slate-900">{prompt.text}</p>
              {speech.supported && speech.enabled && (
                <Button
                  variant="ghost"
                  size="sm"
                  title="Hear the question again"
                  onClick={() => {
                    // A direct tap, so no unlock dance is needed here.
                    speech.unlock()
                    void repeatPrompt()
                  }}
                  disabled={speech.speaking}
                >
                  {speech.speaking ? 'Speaking…' : '🔊 Read aloud'}
                </Button>
              )}
            </div>

            {speech.supported && speech.enabled && !speech.everSpoke && (
              <p className="mt-2 text-xs text-amber-700">
                No sound yet? On iPhone the side switch mutes this — flick it off silent,
                then press <strong>Read aloud</strong>.
              </p>
            )}

            {/* The examiner hands the investigations over as they ask about
                them. Side by side where there is room: an OCT read without its
                angiogram is half the question. */}
            {prompt.figures.length > 0 && (
              <div
                className={cx(
                  'mt-4 gap-3',
                  prompt.figures.length > 1
                    ? 'grid max-w-3xl sm:grid-cols-2'
                    : 'max-w-md',
                )}
              >
                {prompt.figures.map((figure) => (
                  <StationFigureView key={figure.id} figure={figure} />
                ))}
              </div>
            )}

            <div className="mt-5 rounded-lg border border-slate-200 bg-slate-50 p-4">
              <div className="flex items-center gap-3">
                <span
                  className={cx(
                    'flex h-3 w-3 shrink-0 rounded-full',
                    rec.recording ? 'animate-pulse bg-red-500' : 'bg-slate-300',
                  )}
                />
                <span className="text-sm font-medium text-slate-700">
                  {rec.recording ? 'Listening — speak your answer' : 'Not recording'}
                </span>
                {uploading && (
                  <span className="ml-auto text-xs text-slate-500">
                    Uploading answer {uploading}…
                  </span>
                )}
              </div>

              <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-slate-200">
                <div
                  className={cx(
                    'h-full rounded-full transition-[width] duration-100',
                    rec.level > 0.02 ? 'bg-emerald-500' : 'bg-slate-300',
                  )}
                  style={{ width: `${Math.min(100, Math.round(rec.level * 180))}%` }}
                />
              </div>
              <p className="mt-1.5 text-xs text-slate-500">
                {rec.level > 0.02
                  ? 'Microphone is picking you up.'
                  : 'Speak up — the meter should move as you talk.'}
              </p>
            </div>

            <div className="mt-4 flex items-center justify-between">
              <span className="text-xs text-slate-500">
                Suggested time for this question: {Math.round(prompt.seconds / 15) * 15}s
              </span>
              <Button onClick={nextPrompt}>
                {index + 1 < prompts.length ? 'Next question →' : 'Finish station'}
              </Button>
            </div>
          </Card>

          <div className="flex flex-wrap gap-1.5">
            {prompts.map((p, i) => (
              <span
                key={p.label}
                className={cx(
                  'rounded px-2 py-1 text-xs font-medium',
                  i === index
                    ? 'bg-clinical-600 text-white'
                    : i < index
                      ? 'bg-emerald-100 text-emerald-800'
                      : 'bg-slate-100 text-slate-500',
                )}
              >
                {p.label}
              </span>
            ))}
          </div>
        </>
      )}

      {stage === 'review' && (
        <>
          <Alert tone="info" title="Check the transcripts before marking">
            Speech recognition mishears ophthalmic terms, and on a quiet recording it can
            invent whole sentences. Delete anything you did not say and correct anything
            wrong — what is here is exactly what gets marked.
          </Alert>

          {prompts.map((p) => {
            const current = edits[p.label] ?? p.transcript_edited ?? p.transcript ?? ''
            return (
              <Card key={p.label} title={`${p.label}. ${p.text}`}>
                {p.transcription_status === 'pending' && (
                  <p className="text-sm text-slate-500">Transcribing…</p>
                )}
                {p.transcription_status === 'failed' && (
                  <Alert tone="error">
                    Transcription failed: {p.transcription_error ?? 'unknown error'}. You can
                    type what you said below.
                  </Alert>
                )}
                {p.transcription_status === 'complete' && p.transcription_error && (
                  <Alert tone="warning" title="This may not be what you said">
                    {p.transcription_error}
                  </Alert>
                )}
                <Textarea
                  rows={4}
                  className="mt-2 font-sans text-sm"
                  value={current}
                  placeholder="Nothing was transcribed for this question."
                  onChange={(e) => setEdits((prev) => ({ ...prev, [p.label]: e.target.value }))}
                  // saveEdit reports its own failure; swallow the rejection so
                  // it does not surface as an unhandled promise.
                  onBlur={() => void saveEdit(p.label).catch(() => {})}
                />
                {saveFailed.includes(p.label) && (
                  <p className="mt-1 text-xs font-medium text-red-600">
                    Not saved — this correction will be lost. Click away and back to retry.
                  </p>
                )}
              </Card>
            )
          })}

          <div className="flex items-center justify-between">
            <Button variant="secondary" onClick={load}>
              Refresh transcripts
            </Button>
            <Button onClick={submit} loading={submitting}>
              Submit for marking
            </Button>
          </div>
        </>
      )}
    </div>
  )
}
