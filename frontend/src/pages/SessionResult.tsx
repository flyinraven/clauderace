import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { Alert, Badge, Button, Card, Loading, ProgressBar, cx } from '../components/ui'

interface BreakdownItem {
  point_id: number
  point_text: string
  marks: number
  awarded: number
  comment: string | null
  is_critical?: boolean
}

interface ExaminerGrade {
  pass: number
  awarded: number
  feedback: string | null
  breakdown: BreakdownItem[] | null
}

interface ResultPart {
  id: number
  label: string | null
  text: string
  marks: number
  your_answer: string
  awarded: number | null
  flagged: boolean
  examiners: ExaminerGrade[]
  model_answer: { id: number; text: string; marks: number; is_critical: boolean; from_examiner_feedback: boolean }[]
}

interface ResultQuestion {
  id: number
  section: string
  question_type: string
  subspecialty: string | null
  topic: string | null
  stem: string
  total_marks: number
  awarded: number | null
  parts: ResultPart[]
}

interface ResultPayload {
  session: { id: number; paper_title: string; submitted_at: string | null }
  grading_status: string
  result: {
    total_awarded: number
    total_available: number
    percentage: number
    cut_score: number | null
    outcome: string | null
    subspecialty_breakdown: Record<string, { awarded: number; available: number; percentage: number }>
    overall_feedback: string | null
    flagged_parts: number[] | null
    ungraded_parts: number[] | null
  } | null
  questions: ResultQuestion[]
}

export default function SessionResult() {
  const { id } = useParams<{ id: string }>()
  const [data, setData] = useState<ResultPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [regrading, setRegrading] = useState(false)

  const load = useCallback(async () => {
    if (!id) return
    try {
      setData(await api<ResultPayload>(`/sessions/${id}/result`))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load your result')
    }
  }, [id])

  useEffect(() => {
    void load()
  }, [load])

  // Marking runs in the background; poll until it lands.
  useEffect(() => {
    if (!data || ['complete', 'failed'].includes(data.grading_status)) return
    const timer = window.setInterval(load, 4000)
    return () => window.clearInterval(timer)
  }, [data?.grading_status, load])

  const regrade = async () => {
    if (!id) return
    setRegrading(true)
    try {
      await api(`/sessions/${id}/grade`, { method: 'POST' })
      await load()
    } finally {
      setRegrading(false)
    }
  }

  if (error) return <Alert tone="error">{error}</Alert>
  if (!data) return <Loading label="Loading your result…" />

  const marking = !['complete', 'failed'].includes(data.grading_status)
  const result = data.result

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link to="/exams" className="text-sm text-clinical-700 hover:underline">
            ← Back to examinations
          </Link>
          <h1 className="mt-1 text-xl font-semibold text-slate-900">{data.session.paper_title}</h1>
          {data.session.submitted_at && (
            <p className="mt-0.5 text-sm text-slate-500">
              Submitted {new Date(data.session.submitted_at).toLocaleString('en-AU')}
            </p>
          )}
        </div>
        <Button variant="secondary" size="sm" onClick={regrade} loading={regrading}>
          Re-mark
        </Button>
      </div>

      {marking && (
        <Card title="Marking in progress">
          <p className="text-sm text-slate-600">
            Each sub-question is marked twice, mirroring the two examiners who mark every RACE
            question. This page refreshes itself.
          </p>
        </Card>
      )}

      {result && (result.ungraded_parts?.length ?? 0) > 0 && (
        <Alert tone="error" title="This paper was only partly marked">
          {result.ungraded_parts!.length} sub-question(s) could not be marked, so the score
          below covers only part of the paper and <strong>no pass/fail verdict has been
          issued</strong>. The usual cause is the AI provider's rate limit. Press{' '}
          <strong>Re-mark</strong> to finish it — already-marked answers are reused.
        </Alert>
      )}

      {result && (
        <>
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <p className="text-xs uppercase tracking-wide text-slate-500">Your score</p>
              <p className="mt-1 text-3xl font-semibold tabular-nums text-slate-900">
                {result.total_awarded}
                <span className="text-lg text-slate-400"> / {result.total_available}</span>
              </p>
              <p className="mt-0.5 text-sm text-slate-500">{result.percentage}%</p>
            </div>

            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <p className="text-xs uppercase tracking-wide text-slate-500">Angoff cut score</p>
              <p className="mt-1 text-3xl font-semibold tabular-nums text-slate-900">
                {result.cut_score ?? '—'}
              </p>
              <p className="mt-0.5 text-sm text-slate-500">Pass standard for this paper</p>
            </div>

            <div
              className={cx(
                'rounded-xl border p-5 shadow-sm',
                result.outcome === 'pass'
                  ? 'border-emerald-200 bg-emerald-50'
                  : result.outcome === 'fail'
                    ? 'border-red-200 bg-red-50'
                    : 'border-amber-200 bg-amber-50',
              )}
            >
              <p className="text-xs uppercase tracking-wide text-slate-500">Outcome</p>
              <p
                className={cx(
                  'mt-1 text-3xl font-semibold capitalize',
                  result.outcome === 'pass'
                    ? 'text-emerald-700'
                    : result.outcome === 'fail'
                      ? 'text-red-700'
                      : 'text-amber-700',
                )}
              >
                {result.outcome ?? 'pending'}
              </p>
              {result.outcome === 'incomplete' ? (
                <p className="mt-0.5 text-sm text-slate-600">Paper not fully marked</p>
              ) : (
                result.cut_score != null && (
                  <p className="mt-0.5 text-sm text-slate-600">
                    {(result.total_awarded - result.cut_score >= 0 ? '+' : '') +
                      (result.total_awarded - result.cut_score).toFixed(1)}{' '}
                    marks vs standard
                  </p>
                )
              )}
            </div>
          </div>

          {result.overall_feedback && <Alert tone="info">{result.overall_feedback}</Alert>}

          {result.flagged_parts && result.flagged_parts.length > 0 && (
            <Alert tone="warning" title={`${result.flagged_parts.length} sub-question(s) flagged`}>
              The two examiners disagreed materially on these. In a real exam a third examiner
              would arbitrate — treat those marks as provisional.
            </Alert>
          )}

          <Card title="Performance by subspecialty">
            <ul className="space-y-3">
              {Object.entries(result.subspecialty_breakdown)
                .sort((a, b) => a[1].percentage - b[1].percentage)
                .map(([name, values]) => (
                  <li key={name}>
                    <div className="mb-1 flex items-center justify-between text-sm">
                      <span className="text-slate-700">{name}</span>
                      <span className="tabular-nums text-slate-500">
                        {values.awarded}/{values.available} ({values.percentage}%)
                      </span>
                    </div>
                    <ProgressBar value={values.percentage / 100} />
                  </li>
                ))}
            </ul>
          </Card>
        </>
      )}

      <Card title="Question by question">
        <ul className="divide-y divide-slate-100">
          {data.questions.map((question, index) => (
            <li key={question.id} className="py-3">
              <button
                type="button"
                className="flex w-full items-start justify-between gap-4 text-left"
                onClick={() => setExpanded(expanded === question.id ? null : question.id)}
              >
                <div className="min-w-0 flex-1">
                  <p className="font-medium text-slate-800">
                    {index + 1}. {question.topic ?? 'Question'}
                  </p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    Part {question.section} · {question.question_type}
                    {question.subspecialty && ` · ${question.subspecialty}`}
                  </p>
                </div>
                <Badge
                  tone={
                    question.awarded == null
                      ? 'slate'
                      : question.awarded / question.total_marks >= 0.5
                        ? 'green'
                        : 'red'
                  }
                >
                  {question.awarded ?? '—'} / {question.total_marks}
                </Badge>
              </button>

              {expanded === question.id && (
                <div className="mt-4 space-y-5 border-l-2 border-slate-100 pl-4">
                  <p className="prose-clinical text-sm">{question.stem}</p>
                  {question.parts.map((part) => (
                    <PartResult key={part.id} part={part} />
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      </Card>
    </div>
  )
}

function PartResult({ part }: { part: ResultPart }) {
  // Merge the two examiners' per-point awards into one view.
  const merged = new Map<number, { text: string; marks: number; awards: number[]; comments: string[] }>()
  for (const examiner of part.examiners) {
    for (const item of examiner.breakdown ?? []) {
      const entry = merged.get(item.point_id) ?? {
        text: item.point_text,
        marks: item.marks,
        awards: [],
        comments: [],
      }
      entry.awards.push(item.awarded)
      if (item.comment) entry.comments.push(item.comment)
      merged.set(item.point_id, entry)
    }
  }

  return (
    <div className="rounded-lg border border-slate-200 p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm font-medium text-slate-900">
          {part.label ? `${part.label}) ` : ''}
          {part.text}
        </p>
        <div className="flex shrink-0 items-center gap-1.5">
          {part.flagged && <Badge tone="amber">Flagged</Badge>}
          <Badge tone="slate">
            {part.awarded ?? '—'} / {part.marks}
          </Badge>
        </div>
      </div>

      <div className="mt-3">
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Your answer</p>
        <p className="mt-1 whitespace-pre-wrap rounded bg-slate-50 p-3 text-sm text-slate-700">
          {part.your_answer.trim() || '(left blank)'}
        </p>
      </div>

      {merged.size > 0 && (
        <div className="mt-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Marking breakdown
          </p>
          <ul className="mt-2 space-y-2">
            {[...merged.entries()].map(([pointId, entry]) => {
              const avg = entry.awards.reduce((a, b) => a + b, 0) / entry.awards.length
              const full = avg >= entry.marks - 0.01
              const none = avg <= 0.01
              return (
                <li key={pointId} className="flex gap-3 text-sm">
                  <span
                    className={cx(
                      'mt-0.5 min-w-14 shrink-0 rounded px-1.5 py-0.5 text-center text-xs font-semibold tabular-nums ring-1 ring-inset',
                      full
                        ? 'bg-emerald-50 text-emerald-800 ring-emerald-200'
                        : none
                          ? 'bg-red-50 text-red-700 ring-red-200'
                          : 'bg-amber-50 text-amber-800 ring-amber-200',
                    )}
                  >
                    {avg.toFixed(1)}/{entry.marks}
                  </span>
                  <div className="min-w-0">
                    <p className="text-slate-800">{entry.text}</p>
                    {entry.comments[0] && (
                      <p className="mt-0.5 text-xs italic text-slate-500">{entry.comments[0]}</p>
                    )}
                  </div>
                </li>
              )
            })}
          </ul>
        </div>
      )}

      {part.examiners.some((e) => e.feedback) && (
        <div className="mt-3 space-y-2">
          {part.examiners
            .filter((e) => e.feedback)
            .map((examiner) => (
              <div key={examiner.pass} className="rounded bg-clinical-50 p-3">
                <p className="text-xs font-semibold text-clinical-800">
                  Examiner {examiner.pass} — {examiner.awarded}/{part.marks}
                </p>
                <p className="mt-0.5 text-sm text-slate-700">{examiner.feedback}</p>
              </div>
            ))}
        </div>
      )}
    </div>
  )
}
