import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { Alert, Badge, Button, Card, Loading, cx } from '../components/ui'
import { StationFigureView, type StationFigure } from '../components/StationFigureView'

interface BreakdownItem {
  index: number
  text: string
  marks: number
  awarded: number
  comment: string | null
  is_critical: boolean
  // What a full-mark answer says. Written for the question, not for this
  // sitting, so it is the same for everyone who meets the station.
  model_answer: string | null
}

interface PromptResult {
  label: string
  text: string
  marks: number
  awarded: number | null
  transcript: string
  // Set when the transcript could not be trusted. A zero explained by the
  // transcriber having failed is a different fact from a zero the candidate
  // earned, and the result page is where that distinction matters.
  transcription_error: string | null
  // What was on screen when this question was asked. A mark can only be read
  // against the picture it was given for.
  figures: StationFigure[]
  flagged: boolean
  examiners: { pass: number; awarded: number; feedback: string | null; breakdown: BreakdownItem[] | null }[]
}

interface Payload {
  id: number
  circuit_id: number | null
  station: {
    id: number
    station_number: number | null
    station_label: string | null
    exam_period: string | null
    subspecialty: string | null
    title: string | null
    diagnosis: string | null
    findings: string | null
    findings_elicited: string | null
    common_mistakes: string[] | null
    cohort_performance: string | null
    aims: string[] | null
    figures: StationFigure[]
  }
  grading_status: string
  result: {
    total_awarded: number
    total_available: number
    percentage: number
    cut_score: number | null
    outcome: string | null
    overall_feedback: string | null
    flagged_prompts: string[] | null
    ungraded_prompts: string[] | null
  } | null
  prompts: PromptResult[]
}

export default function OsceResult() {
  const { id } = useParams<{ id: string }>()
  const [data, setData] = useState<Payload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [regrading, setRegrading] = useState(false)

  const load = useCallback(async () => {
    if (!id) return
    try {
      setData(await api<Payload>(`/osce/sittings/${id}/result`))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load the result')
    }
  }, [id])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!data || ['complete', 'failed'].includes(data.grading_status)) return
    const timer = window.setInterval(load, 4000)
    return () => window.clearInterval(timer)
  }, [data?.grading_status, load])

  if (error) return <Alert tone="error">{error}</Alert>
  if (!data) return <Loading label="Loading your result…" />

  const marking = !['complete', 'failed', 'partial'].includes(data.grading_status)
  const result = data.result
  // How the paper names it, so a station can be found in the report it came
  // from - "2024 Semester 1 station 13" rather than "Neuro-ophthalmology".
  const printed = data.station.station_label ?? data.station.station_number ?? `#${data.station.id}`
  const stationName = `${data.station.exam_period ? `${data.station.exam_period} ` : ''}station ${printed}`

  const regrade = async () => {
    setRegrading(true)
    try {
      await api(`/osce/sittings/${id}/grade`, { method: 'POST' })
      await load()
    } finally {
      setRegrading(false)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link
            to={data.circuit_id ? `/osce/circuits/${data.circuit_id}/result` : '/osce'}
            className="text-sm text-clinical-700 hover:underline"
          >
            {data.circuit_id ? '← Back to this circuit' : '← Back to OSCE practice'}
          </Link>
          <h1 className="mt-1 text-xl font-semibold text-slate-900">
            {stationName && <span className="text-slate-500">{stationName} · </span>}
            {data.station.title ?? data.station.subspecialty ?? 'OSCE station'}
          </h1>
          <p className="text-sm text-slate-500">{data.station.subspecialty}</p>
        </div>
        <Button variant="secondary" size="sm" onClick={regrade} loading={regrading}>
          Re-mark
        </Button>
      </div>

      {marking && (
        <Card title="Marking in progress">
          <p className="text-sm text-slate-600">
            Each answer is marked twice against the station rubric. This page refreshes itself.
          </p>
        </Card>
      )}

      {result && (result.ungraded_prompts?.length ?? 0) > 0 && (
        <Alert tone="error" title="This station was only partly marked">
          {result.ungraded_prompts!.length} question(s) could not be marked, so no pass/fail
          verdict has been issued. Press <strong>Re-mark</strong> to finish it.
        </Alert>
      )}

      {result && (
        <>
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <p className="text-xs uppercase tracking-wide text-slate-500">Score</p>
              <p className="mt-1 text-3xl font-semibold tabular-nums text-slate-900">
                {result.total_awarded}
                <span className="text-lg text-slate-400"> / {result.total_available}</span>
              </p>
              <p className="mt-0.5 text-sm text-slate-500">{result.percentage}%</p>
            </div>
            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <p className="text-xs uppercase tracking-wide text-slate-500">Pass standard</p>
              <p className="mt-1 text-3xl font-semibold tabular-nums text-slate-900">
                {result.cut_score ?? '—'}
              </p>
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
            </div>
          </div>

          {result.overall_feedback && <Alert tone="info">{result.overall_feedback}</Alert>}
        </>
      )}

      {data.station.findings_elicited && (
        <Card
          title="The signs you were meant to find"
          description="Withheld during the station — this is what you were being asked to elicit."
        >
          <p className="prose-clinical">{data.station.findings_elicited}</p>
        </Card>
      )}

      {data.station.figures?.length > 0 && (
        <Card title="The patient you examined">
          <div className="grid gap-3 sm:grid-cols-2">
            {data.station.figures.map((figure) => (
              <StationFigureView key={figure.id} figure={figure} />
            ))}
          </div>
        </Card>
      )}

      {data.station.diagnosis && (
        <Card title="The diagnosis">
          <p className="prose-clinical">{data.station.diagnosis}</p>
          {data.station.common_mistakes && data.station.common_mistakes.length > 0 && (
            <div className="mt-3">
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                What the real cohort got wrong
              </p>
              <ul className="mt-1 list-inside list-disc space-y-1 text-sm text-slate-600">
                {data.station.common_mistakes.map((m, i) => (
                  <li key={i}>{m}</li>
                ))}
              </ul>
            </div>
          )}
          {data.station.aims && data.station.aims.length > 0 && (
            <div className="mt-3">
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                What the station was testing
              </p>
              <ul className="mt-1 list-inside list-disc space-y-1 text-sm text-slate-600">
                {data.station.aims.map((a, i) => (
                  <li key={i}>{a}</li>
                ))}
              </ul>
            </div>
          )}
          {data.station.cohort_performance && (
            <div className="mt-3">
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                How the real cohort performed
              </p>
              <p className="mt-1 text-sm text-slate-600">{data.station.cohort_performance}</p>
            </div>
          )}
        </Card>
      )}

      {data.prompts.map((prompt) => (
        <Card
          key={prompt.label}
          title={`${prompt.label}. ${prompt.text}`}
          actions={
            <div className="flex gap-1.5">
              {prompt.flagged && <Badge tone="amber">Flagged</Badge>}
              <Badge
                tone={
                  prompt.awarded == null
                    ? 'slate'
                    : prompt.awarded / Math.max(1, prompt.marks) >= 0.5
                      ? 'green'
                      : 'red'
                }
              >
                {prompt.awarded ?? '—'} / {prompt.marks}
              </Badge>
            </div>
          }
        >
          {prompt.figures?.length > 0 && (
            <div className="mb-3 grid gap-3 sm:grid-cols-2">
              {prompt.figures.map((figure) => (
                <StationFigureView key={figure.id} figure={figure} />
              ))}
            </div>
          )}

          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            What you said
          </p>
          <p className="mt-1 whitespace-pre-wrap rounded bg-slate-50 p-3 text-sm text-slate-700">
            {prompt.transcript || '(nothing recorded)'}
          </p>

          {prompt.transcription_error && (
            <div className="mt-2">
              <Alert tone="warning" title="This mark was given against a faulty transcript">
                {prompt.transcription_error}
              </Alert>
            </div>
          )}

          {prompt.examiners[0]?.breakdown && (
            <ul className="mt-3 space-y-2">
              {prompt.examiners[0].breakdown.map((item) => {
                const awards = prompt.examiners
                  .map((e) => e.breakdown?.find((b) => b.index === item.index)?.awarded ?? 0)
                const avg = awards.reduce((a, b) => a + b, 0) / Math.max(1, awards.length)
                const full = avg >= item.marks - 0.01
                return (
                  <li key={item.index} className="flex gap-3 text-sm">
                    <span
                      className={cx(
                        'mt-0.5 min-w-14 shrink-0 rounded px-1.5 py-0.5 text-center text-xs font-semibold tabular-nums ring-1 ring-inset',
                        full
                          ? 'bg-emerald-50 text-emerald-800 ring-emerald-200'
                          : avg <= 0.01
                            ? 'bg-red-50 text-red-700 ring-red-200'
                            : 'bg-amber-50 text-amber-800 ring-amber-200',
                      )}
                    >
                      {avg.toFixed(1)}/{item.marks}
                    </span>
                    <div>
                      <p className="text-slate-800">
                        {item.text}
                        {item.is_critical && (
                          <span className="ml-2 align-middle">
                            <Badge tone="amber">Critical</Badge>
                          </span>
                        )}
                      </p>
                      {item.comment && (
                        <p className="mt-0.5 text-xs italic text-slate-500">{item.comment}</p>
                      )}
                      {item.model_answer && (
                        <p className="mt-1.5 rounded border-l-2 border-sky-300 bg-sky-50/60 py-1 pl-2 text-xs text-slate-700">
                          <span className="font-semibold uppercase tracking-wide text-sky-800">
                            Model answer
                          </span>
                          <span className="ml-1.5">{item.model_answer}</span>
                        </p>
                      )}
                    </div>
                  </li>
                )
              })}
            </ul>
          )}

          {prompt.examiners.some((e) => e.feedback) && (
            <div className="mt-3 space-y-2">
              {prompt.examiners
                .filter((e) => e.feedback)
                .map((e) => (
                  <div key={e.pass} className="rounded bg-clinical-50 p-3">
                    <p className="text-xs font-semibold text-clinical-800">
                      Examiner {e.pass} — {e.awarded}/{prompt.marks}
                    </p>
                    <p className="mt-0.5 text-sm text-slate-700">{e.feedback}</p>
                  </div>
                ))}
            </div>
          )}
        </Card>
      ))}
    </div>
  )
}
