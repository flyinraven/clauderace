import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { formatDuration, useExamClock } from '../hooks/useExamClock'
import { useImage } from '../hooks/useImage'
import { Alert, Badge, Button, Card, Loading, Textarea, cx } from '../components/ui'

interface ExamFigure {
  id: number
  label: string | null
  caption: string | null
  image_id: number | null
}

interface ExamPart {
  id: number
  label: string | null
  text: string
  marks: number
  preamble: string | null
  answer: string
}

interface ExamQuestion {
  id: number
  position: number
  question_type: string
  subspecialty: string | null
  topic: string | null
  stem: string
  total_marks: number
  figures: ExamFigure[]
  parts: ExamPart[]
}

interface SessionPayload {
  session: {
    id: number
    paper_title: string
    paper_number: number | null
    phase: string
    is_timed: boolean
    submitted_at: string | null
  }
  paper: { id: number; title: string; total_marks: number; description: string | null } | null
  sections: { A: ExamQuestion[]; B: ExamQuestion[] } | null
  reading_notes: string
  locked_reason?: string
}

const AUTOSAVE_MS = 10_000
const DRAFT_KEY = (id: number) => `race.draft.${id}`

export default function ExamSession() {
  const { id } = useParams<{ id: string }>()
  const sessionId = id ? Number(id) : null
  const navigate = useNavigate()

  const [payload, setPayload] = useState<SessionPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [answers, setAnswers] = useState<Record<number, string>>({})
  const [notes, setNotes] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [submitting, setSubmitting] = useState(false)

  const { clock, phaseLeft, totalLeft, resync } = useExamClock(sessionId)
  const dirty = useRef(false)
  const lastLoadedPhase = useRef<string | null>(null)

  // --- Load ---------------------------------------------------------------
  const load = useCallback(async () => {
    if (!sessionId) return
    try {
      const data = await api<SessionPayload>(`/sessions/${sessionId}`)
      setPayload(data)
      setNotes((prev) => prev || data.reading_notes || '')

      const server: Record<number, string> = {}
      for (const q of [...(data.sections?.A ?? []), ...(data.sections?.B ?? [])]) {
        for (const part of q.parts) server[part.id] = part.answer
      }
      // A draft newer than the server copy wins — it exists only because a
      // save failed, typically because the instance was asleep.
      const draft = localStorage.getItem(DRAFT_KEY(sessionId))
      if (draft) {
        try {
          const parsed = JSON.parse(draft) as Record<number, string>
          for (const [partId, text] of Object.entries(parsed)) {
            if (text && text.length > (server[Number(partId)] ?? '').length) {
              server[Number(partId)] = text
            }
          }
        } catch {
          /* ignore an unreadable draft */
        }
      }
      setAnswers(server)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load this sitting')
    }
  }, [sessionId])

  useEffect(() => {
    void load()
  }, [load])

  // The paper is withheld until the reading phase, so reload on that transition.
  useEffect(() => {
    if (!clock) return
    if (lastLoadedPhase.current && lastLoadedPhase.current !== clock.phase) void load()
    lastLoadedPhase.current = clock.phase
  }, [clock?.phase, load])

  // --- Save ---------------------------------------------------------------
  const save = useCallback(
    async (force = false) => {
      if (!sessionId || (!dirty.current && !force)) return
      if (!clock?.can_write_answers && !clock?.can_take_notes) return

      const body = {
        answers: clock?.can_write_answers
          ? Object.entries(answers).map(([partId, text]) => ({ part_id: Number(partId), text }))
          : [],
        reading_notes: notes,
      }
      setSaveState('saving')
      try {
        await api(`/sessions/${sessionId}/answers`, { method: 'PUT', body })
        dirty.current = false
        localStorage.removeItem(DRAFT_KEY(sessionId))
        setSaveState('saved')
      } catch {
        // Keep a local copy so nothing is lost while the server is unreachable.
        localStorage.setItem(DRAFT_KEY(sessionId), JSON.stringify(answers))
        setSaveState('error')
      }
    },
    [sessionId, answers, notes, clock],
  )

  useEffect(() => {
    const timer = window.setInterval(() => void save(), AUTOSAVE_MS)
    return () => window.clearInterval(timer)
  }, [save])

  // Flush on tab hide — closing the laptop must not lose the last minute.
  useEffect(() => {
    const onHide = () => {
      if (dirty.current && sessionId) {
        localStorage.setItem(DRAFT_KEY(sessionId), JSON.stringify(answers))
        void save()
      }
    }
    window.addEventListener('pagehide', onHide)
    document.addEventListener('visibilitychange', onHide)
    return () => {
      window.removeEventListener('pagehide', onHide)
      document.removeEventListener('visibilitychange', onHide)
    }
  }, [answers, save, sessionId])

  const begin = async () => {
    if (!sessionId) return
    await api(`/sessions/${sessionId}/begin`, { method: 'POST' })
    await resync()
    await load()
  }

  const submit = async () => {
    if (!sessionId) return
    const unanswered = allParts.filter((p) => !(answers[p.id] ?? '').trim()).length
    const warning = unanswered > 0 ? `\n\n${unanswered} sub-question(s) are still blank.` : ''
    if (!confirm(`Submit this paper? You cannot make further changes.${warning}`)) return

    setSubmitting(true)
    try {
      await save(true)
      await api(`/sessions/${sessionId}/submit`, { method: 'POST' })
      navigate(`/sessions/${sessionId}/result`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Submission failed')
      setSubmitting(false)
    }
  }

  const questions = useMemo(
    () => [...(payload?.sections?.A ?? []), ...(payload?.sections?.B ?? [])],
    [payload],
  )
  const allParts = useMemo(() => questions.flatMap((q) => q.parts), [questions])
  const answeredCount = allParts.filter((p) => (answers[p.id] ?? '').trim()).length

  if (error) return <Alert tone="error">{error}</Alert>
  if (!payload || !clock) return <Loading label="Opening your paper…" />

  const active = questions[activeIndex]
  const urgent = clock.phase === 'writing' && totalLeft <= 300

  return (
    <div className="space-y-4">
      {/* Clock bar */}
      <div className="sticky top-0 z-20 -mx-4 border-b border-slate-200 bg-white/95 px-4 py-3 backdrop-blur sm:-mx-6 sm:px-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-sm font-semibold text-slate-900">{payload.session.paper_title}</p>
            <p className="text-xs text-slate-500">
              {payload.paper?.description ?? ''}
              {payload.session.is_timed ? '' : ' · untimed practice'}
            </p>
          </div>

          <div className="flex items-center gap-4">
            <PhaseIndicator phase={clock.phase} />
            {['prep', 'reading', 'writing'].includes(clock.phase) && (
              <div className="text-right">
                <p
                  className={cx(
                    'font-mono text-2xl font-semibold tabular-nums',
                    urgent ? 'text-red-600' : 'text-slate-900',
                  )}
                >
                  {formatDuration(phaseLeft)}
                </p>
                <p className="text-[11px] uppercase tracking-wide text-slate-500">
                  {clock.phase === 'writing'
                    ? 'writing time left'
                    : clock.phase === 'reading'
                      ? 'reading time left'
                      : 'preparation'}
                </p>
              </div>
            )}
            {clock.can_write_answers && (
              <Button variant="secondary" size="sm" onClick={submit} loading={submitting}>
                Submit paper
              </Button>
            )}
          </div>
        </div>
      </div>

      {urgent && (
        <Alert tone="warning" title="Five minutes remaining">
          Answers save automatically. Finish your current sub-question.
        </Alert>
      )}

      {clock.phase === 'not_started' && (
        <Card title="Ready to begin">
          <p className="text-sm text-slate-600">
            Once you start, the clock runs continuously: {clock.can_view_questions ? '' : '5 minutes preparation, '}
            15 minutes reading (the paper is visible but answers are locked), then the writing period.
            You cannot pause it.
          </p>
          <div className="mt-4">
            <Button onClick={begin}>Start the paper</Button>
          </div>
        </Card>
      )}

      {clock.phase === 'prep' && (
        <Card title="Preparation">
          <p className="text-sm text-slate-600">
            Desktop check. The paper opens in {formatDuration(phaseLeft)}.
          </p>
        </Card>
      )}

      {clock.phase === 'submitted' && (
        <Alert tone="success" title="Paper submitted">
          Your answers have been recorded.
        </Alert>
      )}

      {clock.phase === 'expired' && !payload.session.submitted_at && (
        <Alert tone="warning" title="Writing time has ended">
          Submit now to have your paper marked.
          <div className="mt-2">
            <Button size="sm" onClick={submit} loading={submitting}>
              Submit paper
            </Button>
          </div>
        </Alert>
      )}

      {clock.can_view_questions && questions.length > 0 && active && (
        <div className="grid gap-4 lg:grid-cols-[220px_1fr]">
          {/* Navigator */}
          <aside className="lg:sticky lg:top-28 lg:self-start">
            <Card title={`Questions (${answeredCount}/${allParts.length} answered)`}>
              <div className="grid grid-cols-6 gap-1.5 lg:grid-cols-5">
                {questions.map((question, index) => {
                  const done = question.parts.every((p) => (answers[p.id] ?? '').trim())
                  const partial = question.parts.some((p) => (answers[p.id] ?? '').trim())
                  return (
                    <button
                      key={question.id}
                      type="button"
                      onClick={() => setActiveIndex(index)}
                      title={question.topic ?? undefined}
                      className={cx(
                        'aspect-square rounded-md text-xs font-medium transition',
                        index === activeIndex
                          ? 'bg-clinical-600 text-white'
                          : done
                            ? 'bg-emerald-100 text-emerald-800 hover:bg-emerald-200'
                            : partial
                              ? 'bg-amber-100 text-amber-800 hover:bg-amber-200'
                              : 'bg-slate-100 text-slate-600 hover:bg-slate-200',
                      )}
                    >
                      {index + 1}
                    </button>
                  )
                })}
              </div>
              <p className="mt-3 text-[11px] text-slate-500">
                Part A: {payload.sections?.A.length ?? 0} SEQ · Part B: {payload.sections?.B.length ?? 0} VSAQ
              </p>
            </Card>

            {clock.can_take_notes && (
              <div className="mt-4">
                <Card title="Notes">
                  <Textarea
                    rows={8}
                    value={notes}
                    onChange={(e) => {
                      setNotes(e.target.value)
                      dirty.current = true
                    }}
                    onBlur={() => void save()}
                    placeholder="Plan your answers here…"
                    className="text-xs"
                  />
                </Card>
              </div>
            )}
          </aside>

          {/* Question */}
          <div className="space-y-4">
            <QuestionView
              question={active}
              index={activeIndex}
              total={questions.length}
              answers={answers}
              readOnly={!clock.can_write_answers}
              onChange={(partId, text) => {
                setAnswers((prev) => ({ ...prev, [partId]: text }))
                dirty.current = true
              }}
              onBlur={() => void save()}
            />

            <div className="flex items-center justify-between">
              <Button
                variant="secondary"
                size="sm"
                disabled={activeIndex === 0}
                onClick={() => setActiveIndex((i) => i - 1)}
              >
                ← Previous
              </Button>
              <SaveIndicator state={saveState} readOnly={!clock.can_write_answers} />
              <Button
                variant="secondary"
                size="sm"
                disabled={activeIndex >= questions.length - 1}
                onClick={() => setActiveIndex((i) => i + 1)}
              >
                Next →
              </Button>
            </div>
          </div>
        </div>
      )}

      {clock.can_view_questions && questions.length === 0 && (
        <Alert tone="warning">This paper has no questions attached.</Alert>
      )}
    </div>
  )
}

function PhaseIndicator({ phase }: { phase: string }) {
  const map: Record<string, { label: string; tone: 'slate' | 'amber' | 'green' | 'blue' | 'red' }> = {
    not_started: { label: 'Not started', tone: 'slate' },
    prep: { label: 'Preparation', tone: 'slate' },
    reading: { label: 'Reading — answers locked', tone: 'amber' },
    writing: { label: 'Writing', tone: 'green' },
    submitted: { label: 'Submitted', tone: 'blue' },
    expired: { label: 'Time expired', tone: 'red' },
  }
  const entry = map[phase] ?? { label: phase, tone: 'slate' as const }
  return <Badge tone={entry.tone}>{entry.label}</Badge>
}

function SaveIndicator({ state, readOnly }: { state: string; readOnly: boolean }) {
  if (readOnly) return <span className="text-xs text-slate-400">Answers locked</span>
  const text = {
    idle: '',
    saving: 'Saving…',
    saved: 'All changes saved',
    error: 'Saved locally — will retry',
  }[state]
  return (
    <span className={cx('text-xs', state === 'error' ? 'text-amber-600' : 'text-slate-400')}>
      {text}
    </span>
  )
}

function QuestionView({
  question,
  index,
  total,
  answers,
  readOnly,
  onChange,
  onBlur,
}: {
  question: ExamQuestion
  index: number
  total: number
  answers: Record<number, string>
  readOnly: boolean
  onChange: (partId: number, text: string) => void
  onBlur: () => void
}) {
  return (
    <Card
      title={`Question ${index + 1} of ${total}`}
      description={question.topic ?? undefined}
      actions={
        <div className="flex gap-1.5">
          <Badge tone="blue">{question.question_type}</Badge>
          <Badge tone="slate">{question.total_marks} marks</Badge>
        </div>
      }
    >
      <p className="prose-clinical">{question.stem}</p>

      {question.figures.length > 0 && (
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          {question.figures.map((figure) => (
            <ExamFigureView key={figure.id} figure={figure} />
          ))}
        </div>
      )}

      <div className="mt-6 space-y-6">
        {question.parts.map((part) => {
          const value = answers[part.id] ?? ''
          const words = value.trim() ? value.trim().split(/\s+/).length : 0
          return (
            <div key={part.id}>
              {part.preamble && (
                <p className="mb-2 rounded-lg bg-slate-50 px-3 py-2 text-sm italic text-slate-600">
                  {part.preamble}
                </p>
              )}
              <div className="flex items-start justify-between gap-4">
                <p className="prose-clinical flex-1 font-medium text-slate-900">
                  {part.label ? `${part.label}) ` : ''}
                  {part.text}
                </p>
                <Badge tone="slate">{part.marks} marks</Badge>
              </div>
              <Textarea
                rows={Math.max(4, Math.round(part.marks * 1.6))}
                value={value}
                readOnly={readOnly}
                disabled={readOnly}
                onChange={(e) => onChange(part.id, e.target.value)}
                onBlur={onBlur}
                placeholder={readOnly ? 'Answers unlock when writing time begins' : 'Your answer…'}
                className="mt-2 font-sans text-sm"
              />
              <p className="mt-1 text-right text-xs text-slate-400">{words} words</p>
            </div>
          )
        })}
      </div>
    </Card>
  )
}

function ExamFigureView({ figure }: { figure: ExamFigure }) {
  const { url } = useImage(figure.image_id)
  const [zoomed, setZoomed] = useState(false)

  return (
    <figure className="overflow-hidden rounded-lg border border-slate-200">
      {url ? (
        <button type="button" className="block w-full cursor-zoom-in" onClick={() => setZoomed(true)}>
          <img src={url} alt={figure.caption ?? 'Clinical figure'} className="w-full" />
        </button>
      ) : (
        <div className="flex h-40 items-center justify-center bg-slate-50 text-xs text-slate-400">
          {figure.image_id ? 'Loading…' : 'No image'}
        </div>
      )}
      <figcaption className="border-t border-slate-200 px-3 py-1.5 text-xs text-slate-600">
        <span className="font-medium">{figure.label}</span>
        {figure.caption && `: ${figure.caption}`}
      </figcaption>
      {zoomed && url && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/85 p-6"
          onClick={() => setZoomed(false)}
          role="presentation"
        >
          <img src={url} alt="" className="max-h-full max-w-full rounded-lg" />
        </div>
      )}
    </figure>
  )
}
