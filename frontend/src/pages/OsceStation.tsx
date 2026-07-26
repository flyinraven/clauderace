import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { useImage } from '../hooks/useImage'
import { useRecorder } from '../hooks/useRecorder'
import { formatDuration } from '../hooks/useExamClock'
import { Alert, Badge, Button, Card, Loading, Textarea, cx } from '../components/ui'

interface OscePrompt {
  label: string
  index: number
  text: string
  seconds: number
  marks: number
  transcript: string | null
  transcript_edited: string | null
  transcription_status: string
  transcription_error: string | null
}

interface StationFigure {
  id: number
  image_id: number | null
  caption: string | null
  position: number
}

interface Sitting {
  id: number
  station: {
    id: number
    subspecialty: string | null
    title: string | null
    case_summary: string | null
    patient_history: string | null
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
  const [submitting, setSubmitting] = useState(false)

  const rec = useRecorder()
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
      const ext = mimeType.includes('webm') ? 'webm' : 'm4a'
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

  /** Finish this answer and move on; the upload runs in the background. */
  const nextPrompt = useCallback(async () => {
    if (!prompt) return
    const captured = await rec.stop()
    if (captured) {
      void uploadAnswer(
        prompt.label,
        prompt.index,
        captured.blob,
        captured.mimeType,
        captured.durationMs,
      )
    }
    if (index + 1 < prompts.length) {
      setIndex(index + 1)
      await rec.start()
    } else {
      rec.release()
      await load()
      setStage('review')
    }
  }, [prompt, rec, index, prompts.length, uploadAnswer, load])

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

  const begin = async () => {
    if (!sittingId) return
    const ok = await rec.requestAccess()
    if (!ok) return
    await api(`/osce/sittings/${sittingId}/begin`, { method: 'POST' })
    await load()
    await rec.start()
  }

  const saveEdit = async (label: string) => {
    if (!sittingId) return
    await api(`/osce/sittings/${sittingId}/answers/${label}/transcript`, {
      method: 'PUT',
      body: { transcript: edits[label] ?? '' },
    })
  }

  const submit = async () => {
    if (!sittingId) return
    setSubmitting(true)
    try {
      for (const label of Object.keys(edits)) await saveEdit(label)
      await api(`/osce/sittings/${sittingId}/submit`, { method: 'POST' })
      navigate(`/osce/sittings/${sittingId}/result`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Submission failed')
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
            <p className="text-sm font-semibold text-slate-900">
              {sitting.station.title ?? sitting.station.subspecialty ?? 'OSCE station'}
            </p>
            <p className="text-xs text-slate-500">
              {sitting.station.subspecialty} · {sitting.station.total_marks} marks
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
          <div className="mt-4">
            <Button onClick={begin}>Allow microphone &amp; start</Button>
          </div>
        </Card>
      )}

      {!notStarted && stage === 'sitting' && prompt && (
        <>
          <Card title="The case">
            <p className="prose-clinical">{sitting.station.case_summary}</p>
            {sitting.station.patient_history && (
              <div className="mt-3">
                <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                  History
                </p>
                <p className="prose-clinical mt-1">{sitting.station.patient_history}</p>
              </div>
            )}
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
              Everything else is for you to elicit and describe — the clinical signs are
              deliberately withheld until your result.
            </p>
          </Card>

          <Card
            title={`Question ${prompt.label} of ${prompts.length}`}
            actions={<Badge tone="slate">{prompt.marks} marks</Badge>}
          >
            <p className="text-lg font-medium text-slate-900">{prompt.text}</p>

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
            Speech recognition sometimes mishears ophthalmic terms. Correct anything wrong
            here — you are only fixing what you actually said, and it stops a
            transcription error costing you marks.
          </Alert>

          {prompts.map((p) => {
            const current = edits[p.label] ?? p.transcript_edited ?? p.transcript ?? ''
            return (
              <Card key={p.label} title={`${p.label}. ${p.text}`}>
                {p.transcription_status === 'pending' && (
                  <p className="text-sm text-slate-500">Transcribing…</p>
                )}
                {p.transcription_status === 'failed' && (
                  <Alert tone="warning">
                    Transcription failed: {p.transcription_error ?? 'unknown error'}. You can
                    type what you said below.
                  </Alert>
                )}
                <Textarea
                  rows={4}
                  className="mt-2 font-sans text-sm"
                  value={current}
                  placeholder="Nothing was transcribed for this question."
                  onChange={(e) => setEdits((prev) => ({ ...prev, [p.label]: e.target.value }))}
                  onBlur={() => void saveEdit(p.label)}
                />
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
