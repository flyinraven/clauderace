import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { Alert, Badge, Button, Card, EmptyState, Loading } from '../components/ui'

interface Paper {
  id: number
  title: string
  paper_number: number | null
  day: number | null
  description: string | null
  total_marks: number
  cut_score: number | null
  is_published: boolean
  question_count: number
}

interface Sitting {
  id: number
  paper_id: number
  paper_title: string
  paper_number: number | null
  phase: string
  is_timed: boolean
  started_at: string | null
  submitted_at: string | null
  grading_status: string
  created_at: string
}

export default function Exams() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [papers, setPapers] = useState<Paper[]>([])
  const [sittings, setSittings] = useState<Sitting[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState<number | null>(null)

  const load = useCallback(() => {
    Promise.all([api<Paper[]>('/papers'), api<Sitting[]>('/sessions')])
      .then(([p, s]) => {
        setPapers(p)
        setSittings(s)
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(load, [load])

  const start = async (paperId: number, isTimed: boolean) => {
    setStarting(paperId)
    setError(null)
    try {
      const sitting = await api<Sitting>('/sessions', {
        method: 'POST',
        body: { paper_id: paperId, is_timed: isTimed },
      })
      navigate(`/sessions/${sitting.id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start the paper')
      setStarting(null)
    }
  }

  if (loading) return <Loading label="Loading papers…" />

  const inProgress = sittings.filter((s) => !s.submitted_at)
  const completed = sittings.filter((s) => s.submitted_at)

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Mock examinations</h1>
        <p className="mt-1 text-sm text-slate-500">
          Timings and mark allocations replicate the real RACE written papers.
        </p>
      </div>

      {error && <Alert tone="error">{error}</Alert>}

      {inProgress.length > 0 && (
        <Card title="In progress">
          <ul className="divide-y divide-slate-100">
            {inProgress.map((sitting) => (
              <li key={sitting.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                <div>
                  <p className="font-medium text-slate-800">{sitting.paper_title}</p>
                  <p className="text-xs text-slate-500">
                    {sitting.started_at
                      ? `Started ${new Date(sitting.started_at).toLocaleString('en-AU')}`
                      : 'Not yet started'}
                    {!sitting.is_timed && ' · untimed'}
                  </p>
                </div>
                <Button size="sm" onClick={() => navigate(`/sessions/${sitting.id}`)}>
                  Resume
                </Button>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card
        title="Available papers"
        description={user?.role === 'admin' ? 'Admins also see unpublished papers.' : undefined}
      >
        {papers.length === 0 ? (
          <EmptyState title="No papers available yet">
            {user?.role === 'admin' ? (
              <>
                Assemble one under{' '}
                <Link to="/admin/papers" className="font-medium underline">
                  Papers
                </Link>
                .
              </>
            ) : (
              'Your administrator has not published a paper yet.'
            )}
          </EmptyState>
        ) : (
          <ul className="divide-y divide-slate-100">
            {papers.map((paper) => (
              <li key={paper.id} className="flex flex-wrap items-center justify-between gap-3 py-4">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="font-medium text-slate-900">{paper.title}</p>
                    {!paper.is_published && <Badge tone="amber">Unpublished</Badge>}
                  </div>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {paper.description}
                    {paper.cut_score != null && ` · Angoff cut score ${paper.cut_score} of ${paper.total_marks}`}
                  </p>
                </div>
                <div className="flex gap-2">
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => start(paper.id, false)}
                    loading={starting === paper.id}
                  >
                    Untimed practice
                  </Button>
                  <Button size="sm" onClick={() => start(paper.id, true)} loading={starting === paper.id}>
                    Sit under exam conditions
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {completed.length > 0 && (
        <Card title="Completed">
          <ul className="divide-y divide-slate-100">
            {completed.map((sitting) => (
              <li key={sitting.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                <div>
                  <p className="font-medium text-slate-800">{sitting.paper_title}</p>
                  <p className="text-xs text-slate-500">
                    Submitted {new Date(sitting.submitted_at!).toLocaleString('en-AU')}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Badge
                    tone={
                      sitting.grading_status === 'complete'
                        ? 'green'
                        : sitting.grading_status === 'running' || sitting.grading_status === 'queued'
                          ? 'amber'
                          : 'slate'
                    }
                  >
                    {sitting.grading_status === 'complete' ? 'Marked' : sitting.grading_status}
                  </Badge>
                  <Button size="sm" variant="secondary" onClick={() => navigate(`/sessions/${sitting.id}/result`)}>
                    View result
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  )
}
