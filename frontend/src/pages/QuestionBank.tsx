import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { useJob } from '../hooks/useJob'
import { Alert, Badge, Button, Card, EmptyState, Input, Loading, ProgressBar, Select } from '../components/ui'
import type { QuestionPage, QuestionSummary } from '../types'

interface FilterOptions {
  subspecialties: string[]
  question_types: string[]
  statuses: string[]
  sources: string[]
  exam_periods: string[]
}

const PAGE_SIZE = 25

export default function QuestionBank() {
  const { user } = useAuth()
  const [page, setPage] = useState<QuestionPage | null>(null)
  const [options, setOptions] = useState<FilterOptions | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const [subspecialty, setSubspecialty] = useState('')
  const [questionType, setQuestionType] = useState('')
  const [status, setStatus] = useState('')
  const [answerJob, setAnswerJob] = useState<number | null>(null)
  const [showGenerator, setShowGenerator] = useState(false)
  const [genType, setGenType] = useState('VSAQ')
  const [genCount, setGenCount] = useState(20)
  const [genSubspecialty, setGenSubspecialty] = useState('')
  const [genDifficulty, setGenDifficulty] = useState('')

  const { job } = useJob(answerJob)

  const load = useCallback(() => {
    setLoading(true)
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) })
    if (search.trim()) params.set('search', search.trim())
    if (subspecialty) params.set('subspecialty', subspecialty)
    if (questionType) params.set('question_type', questionType)
    if (status) params.set('status', status)

    api<QuestionPage>(`/questions?${params}`)
      .then(setPage)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [offset, search, subspecialty, questionType, status])

  useEffect(load, [load])

  useEffect(() => {
    api<FilterOptions>('/meta/filters').then(setOptions).catch(() => undefined)
  }, [])

  useEffect(() => {
    if (job && job.status === 'completed') load()
  }, [job?.status, load])

  const generateAnswers = async () => {
    setError(null)
    try {
      const result = await api<{ job_id: number; question_count: number }>(
        '/questions/generate-model-answers',
        { method: 'POST', body: { only_missing: true } },
      )
      setAnswerJob(result.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start generation')
    }
  }

  const generateQuestions = async () => {
    setError(null)
    try {
      const result = await api<{ job_id: number }>('/questions/generate', {
        method: 'POST',
        body: {
          question_type: genType,
          count: genCount,
          subspecialties: genSubspecialty ? [genSubspecialty] : null,
          difficulty: genDifficulty || null,
        },
      })
      setAnswerJob(result.job_id)
      setShowGenerator(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start generation')
    }
  }

  const approveReviewed = async () => {
    if (
      !confirm(
        'Approve every question currently in review that has a model answer?\n\n' +
          'Only approved questions can be used to assemble papers or seen by students.',
      )
    )
      return
    setError(null)
    try {
      const result = await api<{ updated: number }>('/questions/bulk-status', {
        method: 'POST',
        body: { from_status: 'review', to_status: 'approved', require_model_answer: true },
      })
      if (result.updated === 0) {
        setError('Nothing to approve — questions need a model answer first.')
      }
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Bulk approval failed')
    }
  }

  const resetFilters = () => {
    setSearch('')
    setSubspecialty('')
    setQuestionType('')
    setStatus('')
    setOffset(0)
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Question bank</h1>
          <p className="mt-1 text-sm text-slate-500">
            {page ? `${page.total} question${page.total === 1 ? '' : 's'}` : 'Loading…'}
          </p>
        </div>
        {user?.role === 'admin' && (
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" onClick={() => setShowGenerator((v) => !v)}>
              Generate questions
            </Button>
            <Button variant="secondary" onClick={approveReviewed}>
              Approve reviewed
            </Button>
            <Button onClick={generateAnswers}>Generate missing model answers</Button>
          </div>
        )}
      </div>

      {error && <Alert tone="error">{error}</Alert>}

      {showGenerator && user?.role === 'admin' && (
        <Card
          title="Generate new questions"
          description="Written to the bank as drafts in review, each with its marking key. The examiners' reports contain no VSAQs, so Part B of every paper has to be generated."
        >
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <Select value={genType} onChange={(e) => setGenType(e.target.value)}>
              <option value="VSAQ">VSAQ — 2 marks each</option>
              <option value="SEQ">SEQ — 20 marks each</option>
            </Select>
            <Input
              type="number"
              min={1}
              max={200}
              value={genCount}
              onChange={(e) => setGenCount(Number(e.target.value))}
            />
            <Select value={genSubspecialty} onChange={(e) => setGenSubspecialty(e.target.value)}>
              <option value="">All nine (balanced)</option>
              {options?.subspecialties.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
            <Select value={genDifficulty} onChange={(e) => setGenDifficulty(e.target.value)}>
              <option value="">Mixed difficulty</option>
              <option value="easy">Easy</option>
              <option value="moderate">Moderate</option>
              <option value="hard">Hard</option>
            </Select>
            <Button onClick={generateQuestions}>Start</Button>
          </div>
          <p className="mt-3 text-xs text-slate-500">
            A full set of four papers needs 60 VSAQs (15 per paper).
          </p>
        </Card>
      )}

      {job && ['pending', 'running'].includes(job.status) && (
        <Card title="Generating model answers">
          <ProgressBar
            value={job.progress}
            label={job.message ?? `${job.completed_steps} of ${job.total_steps}`}
          />
        </Card>
      )}

      <Card>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Input
            placeholder="Search topic or stem…"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setOffset(0)
            }}
          />
          <Select
            value={subspecialty}
            onChange={(e) => {
              setSubspecialty(e.target.value)
              setOffset(0)
            }}
          >
            <option value="">All subspecialties</option>
            {options?.subspecialties.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
          <Select
            value={questionType}
            onChange={(e) => {
              setQuestionType(e.target.value)
              setOffset(0)
            }}
          >
            <option value="">All types</option>
            {options?.question_types.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </Select>
          {user?.role === 'admin' && (
            <Select
              value={status}
              onChange={(e) => {
                setStatus(e.target.value)
                setOffset(0)
              }}
            >
              <option value="">All statuses</option>
              {options?.statuses.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
          )}
          <Button variant="secondary" onClick={resetFilters}>
            Reset
          </Button>
        </div>
      </Card>

      {loading ? (
        <Loading />
      ) : !page || page.items.length === 0 ? (
        <EmptyState title="No questions found">
          {user?.role === 'admin'
            ? 'Upload an examiners’ report under Documents to populate the bank.'
            : 'Nothing has been published yet.'}
        </EmptyState>
      ) : (
        <>
          <div className="grid gap-3">
            {page.items.map((question) => (
              <QuestionRow key={question.id} question={question} />
            ))}
          </div>

          <div className="flex items-center justify-between">
            <p className="text-sm text-slate-500">
              Showing {page.offset + 1}–{Math.min(page.offset + page.limit, page.total)} of {page.total}
            </p>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                size="sm"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Previous
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={offset + PAGE_SIZE >= page.total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Next
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

function QuestionRow({ question }: { question: QuestionSummary }) {
  return (
    <Link
      to={`/questions/${question.id}`}
      className="block rounded-xl border border-slate-200 bg-white p-4 shadow-sm transition hover:border-clinical-300 hover:shadow"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="font-medium text-slate-900">{question.topic ?? 'Untitled question'}</p>
          <p className="mt-1 text-xs text-slate-500">
            {question.exam_period ?? 'Generated'}
            {question.original_number ? ` · ${question.question_type} ${question.original_number}` : ''}
            {' · '}
            {question.part_count} part{question.part_count === 1 ? '' : 's'}
            {question.figure_count > 0 && ` · ${question.figure_count} figure${question.figure_count === 1 ? '' : 's'}`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone="blue">{question.question_type}</Badge>
          {question.subspecialty && <Badge>{question.subspecialty}</Badge>}
          <Badge tone="slate">{question.total_marks} marks</Badge>
          <Badge
            tone={
              question.model_answer_status === 'complete'
                ? 'green'
                : question.model_answer_status === 'failed'
                  ? 'red'
                  : 'amber'
            }
          >
            {question.model_answer_status === 'complete' ? 'Model answer' : 'No model answer'}
          </Badge>
        </div>
      </div>
    </Link>
  )
}
