import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { useImage } from '../hooks/useImage'
import { useJob } from '../hooks/useJob'
import { Alert, Badge, Button, Card, Loading, ProgressBar } from '../components/ui'
import type { Figure, QuestionDetail as Question, QuestionPart } from '../types'

export default function QuestionDetail() {
  const { id } = useParams<{ id: string }>()
  const { user } = useAuth()
  const [question, setQuestion] = useState<Question | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showAnswers, setShowAnswers] = useState(true)
  const [jobId, setJobId] = useState<number | null>(null)

  const { job } = useJob(jobId)

  const load = () => {
    if (!id) return
    api<Question>(`/questions/${id}`)
      .then(setQuestion)
      .catch((err) => setError(err.message))
  }

  useEffect(load, [id])

  useEffect(() => {
    if (job && job.status === 'completed') load()
  }, [job?.status])

  const regenerate = async () => {
    if (!id) return
    setError(null)
    try {
      const result = await api<{ job_id: number }>('/questions/generate-model-answers', {
        method: 'POST',
        body: { question_ids: [Number(id)] },
      })
      setJobId(result.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start generation')
    }
  }

  if (error) return <Alert tone="error">{error}</Alert>
  if (!question) return <Loading label="Loading question…" />

  const warnings = (question.generation_meta?.warnings ?? []) as string[]
  const answerWarnings = (question.generation_meta?.model_answer_warnings ?? []) as string[]
  const examinerNote = question.generation_meta?.examiner_note as string | undefined

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link to="/bank" className="text-sm text-clinical-700 hover:underline">
            ← Back to the question bank
          </Link>
          <h1 className="mt-1 text-xl font-semibold text-slate-900">
            {question.topic ?? 'Untitled question'}
          </h1>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <Badge tone="blue">{question.question_type}</Badge>
            {question.subspecialty && <Badge>{question.subspecialty}</Badge>}
            <Badge tone="slate">{question.total_marks} marks</Badge>
            {question.exam_period && <Badge tone="violet">{question.exam_period}</Badge>}
            {question.angoff_expected != null && (
              <Badge tone="amber">
                Angoff {(question.angoff_expected * 100).toFixed(0)}%
              </Badge>
            )}
          </div>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" size="sm" onClick={() => setShowAnswers((v) => !v)}>
            {showAnswers ? 'Hide model answers' : 'Show model answers'}
          </Button>
          {user?.role === 'admin' && (
            <Button size="sm" onClick={regenerate}>
              Regenerate model answer
            </Button>
          )}
        </div>
      </div>

      {job && ['pending', 'running'].includes(job.status) && (
        <Card>
          <ProgressBar value={job.progress} label={job.message ?? 'Generating…'} />
        </Card>
      )}

      {warnings.length > 0 && user?.role === 'admin' && (
        <Alert tone="warning" title="Transcription warnings">
          <ul className="mt-1 list-inside list-disc">
            {warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </Alert>
      )}

      {question.purpose && (
        <Card title="Purpose of the question">
          <p className="prose-clinical">{question.purpose}</p>
          {question.curriculum_standard_raw && (
            <p className="mt-3 text-xs text-slate-500">
              <span className="font-medium">Curriculum standard:</span>{' '}
              {question.curriculum_standard_raw}
            </p>
          )}
        </Card>
      )}

      <Card title="Clinical scenario">
        <p className="prose-clinical">{question.stem}</p>
        {question.figures.length > 0 && (
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            {question.figures.map((figure) => (
              <FigureView key={figure.id} figure={figure} />
            ))}
          </div>
        )}
      </Card>

      <div className="space-y-4">
        {question.parts.map((part) => (
          <PartView key={part.id} part={part} showAnswers={showAnswers} />
        ))}
      </div>

      {examinerNote && (
        <Card title="Examiner's overall guidance">
          <p className="prose-clinical">{examinerNote}</p>
        </Card>
      )}

      {question.examiner_feedback.length > 0 && (
        <Card
          title="Examiners' report"
          description="Published commentary on how the real cohort answered this question."
        >
          <div className="grid gap-4 sm:grid-cols-2">
            {question.examiner_feedback.map((feedback, index) => (
              <div key={index} className="rounded-lg border border-slate-200 p-4">
                <p className="text-sm font-semibold text-slate-800">
                  Examiner {feedback.examiner_number ?? index + 1}
                </p>
                {feedback.common_mistakes && feedback.common_mistakes.length > 0 && (
                  <>
                    <p className="mt-2 text-xs font-medium uppercase tracking-wide text-slate-500">
                      Common mistakes
                    </p>
                    <ul className="mt-1 list-inside list-disc space-y-1 text-sm text-slate-600">
                      {feedback.common_mistakes.map((item, i) => (
                        <li key={i}>{item}</li>
                      ))}
                    </ul>
                  </>
                )}
                {feedback.cohort_impression && feedback.cohort_impression.length > 0 && (
                  <>
                    <p className="mt-3 text-xs font-medium uppercase tracking-wide text-slate-500">
                      Impression of the cohort
                    </p>
                    <ul className="mt-1 list-inside list-disc space-y-1 text-sm text-slate-600">
                      {feedback.cohort_impression.map((item, i) => (
                        <li key={i}>{item}</li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            ))}
          </div>
        </Card>
      )}

      {answerWarnings.length > 0 && user?.role === 'admin' && (
        <Alert tone="warning" title="Model answer warnings">
          <ul className="mt-1 list-inside list-disc">
            {answerWarnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </Alert>
      )}
    </div>
  )
}

function PartView({ part, showAnswers }: { part: QuestionPart; showAnswers: boolean }) {
  const pointsTotal = part.answer_points.reduce((sum, p) => sum + p.marks, 0)

  return (
    <Card>
      {part.preamble && (
        <p className="mb-3 rounded-lg bg-slate-50 px-3 py-2 text-sm italic text-slate-600">
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

      {showAnswers && part.answer_points.length > 0 && (
        <div className="mt-4 rounded-lg border border-emerald-200 bg-emerald-50/50 p-4">
          <div className="flex items-center justify-between">
            <p className="text-xs font-semibold uppercase tracking-wide text-emerald-800">
              Model answer — key points
            </p>
            <span className="text-xs tabular-nums text-emerald-700">
              {pointsTotal.toFixed(1).replace(/\.0$/, '')} / {part.marks} marks
            </span>
          </div>
          <ul className="mt-3 space-y-2.5">
            {part.answer_points.map((point) => (
              <li key={point.id} className="flex gap-3">
                <span className="mt-0.5 min-w-9 shrink-0 rounded bg-white px-1.5 py-0.5 text-center text-xs font-semibold tabular-nums text-emerald-800 ring-1 ring-emerald-200">
                  {point.marks % 1 === 0 ? point.marks : point.marks.toFixed(1)}
                </span>
                <div className="min-w-0">
                  <p className="text-sm text-slate-800">
                    {point.text}
                    {point.from_examiner_feedback && (
                      <span className="ml-2 align-middle">
                        <Badge tone="amber">Examiners flagged this</Badge>
                      </span>
                    )}
                  </p>
                  {point.accepted_alternatives && point.accepted_alternatives.length > 0 && (
                    <p className="mt-0.5 text-xs text-slate-500">
                      Also accepted: {point.accepted_alternatives.join('; ')}
                    </p>
                  )}
                  {point.rationale && (
                    <p className="mt-0.5 text-xs italic text-slate-500">{point.rationale}</p>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {showAnswers && part.answer_points.length === 0 && (
        <p className="mt-3 text-sm text-slate-400">No model answer generated yet.</p>
      )}
    </Card>
  )
}

function FigureView({ figure }: { figure: Figure }) {
  const { url, error } = useImage(figure.image_id)
  const [zoomed, setZoomed] = useState(false)

  return (
    <figure className="overflow-hidden rounded-lg border border-slate-200">
      {url ? (
        <button type="button" onClick={() => setZoomed(true)} className="block w-full cursor-zoom-in">
          <img src={url} alt={figure.caption ?? figure.label ?? 'Clinical figure'} className="w-full" />
        </button>
      ) : error ? (
        <div className="flex h-40 items-center justify-center bg-slate-50 px-4 text-center text-xs text-slate-400">
          {error}
        </div>
      ) : figure.image_id ? (
        <div className="flex h-40 items-center justify-center bg-slate-50 text-xs text-slate-400">
          Loading…
        </div>
      ) : (
        <div className="flex h-40 items-center justify-center bg-slate-50 px-4 text-center text-xs text-slate-400">
          No image available{figure.wanted_description ? ` — ${figure.wanted_description}` : ''}
        </div>
      )}
      <figcaption className="border-t border-slate-200 px-3 py-2 text-xs text-slate-600">
        <span className="font-medium">{figure.label}</span>
        {figure.caption && `: ${figure.caption}`}
        {figure.image_description && (
          <span className="mt-1 block italic text-slate-500">{figure.image_description}</span>
        )}
      </figcaption>

      {zoomed && url && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/80 p-6"
          onClick={() => setZoomed(false)}
          role="presentation"
        >
          <img src={url} alt={figure.caption ?? 'Clinical figure'} className="max-h-full max-w-full rounded-lg" />
        </div>
      )}
    </figure>
  )
}
