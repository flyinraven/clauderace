import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api/client'
import { Alert, Badge, Button, Card, EmptyState, Loading } from '../../components/ui'

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

interface Availability {
  approved_seq: number
  approved_vsaq: number
  papers: {
    paper_number: number
    day: number
    seq_required: number
    vsaq_required: number
    can_assemble: boolean
    writing_minutes: number
    total_marks: number
  }[]
}

export default function Papers() {
  const [papers, setPapers] = useState<Paper[]>([])
  const [availability, setAvailability] = useState<Availability | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)

  const load = useCallback(() => {
    Promise.all([api<Paper[]>('/papers'), api<Availability>('/papers/availability')])
      .then(([p, a]) => {
        setPapers(p)
        setAvailability(a)
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(load, [load])

  const assemble = async (paperNumber: number, allowPartial: boolean) => {
    setBusy(paperNumber)
    setError(null)
    setNotice(null)
    try {
      const result = await api<{ paper: Paper; report: Record<string, unknown> }>('/papers/assemble', {
        method: 'POST',
        body: { paper_number: paperNumber, allow_partial: allowPartial, publish: true },
      })
      const shortfalls = (result.report.shortfalls as string[]) ?? []
      setNotice(
        `Assembled "${result.paper.title}" with ${result.report.seq_selected} SEQ and ` +
          `${result.report.vsaq_selected} VSAQ.` +
          (shortfalls.length ? ` Incomplete: ${shortfalls.join('; ')}.` : ''),
      )
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Assembly failed')
    } finally {
      setBusy(null)
    }
  }

  const togglePublish = async (paper: Paper) => {
    await api(`/papers/${paper.id}/publish?published=${!paper.is_published}`, { method: 'POST' })
    load()
  }

  const remove = async (paper: Paper) => {
    if (!confirm(`Delete "${paper.title}"?`)) return
    try {
      await api(`/papers/${paper.id}`, { method: 'DELETE' })
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed')
    }
  }

  if (loading) return <Loading label="Loading papers…" />

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Exam papers</h1>
        <p className="mt-1 text-sm text-slate-500">
          Assemble papers from approved questions. Subspecialties are spread automatically.
        </p>
      </div>

      {error && <Alert tone="error">{error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}

      {availability && (
        <Card
          title="Assemble a paper"
          description={`Bank: ${availability.approved_seq} approved SEQ, ${availability.approved_vsaq} approved VSAQ.`}
        >
          {availability.approved_vsaq === 0 && (
            <div className="mb-4">
              <Alert tone="warning" title="No VSAQs in the bank">
                The examiners' reports contain no VSAQs, so they have to be generated. Until then you
                can still assemble a SEQ-only paper using "Assemble anyway".
              </Alert>
            </div>
          )}
          <div className="grid gap-3 sm:grid-cols-2">
            {availability.papers.map((spec) => (
              <div key={spec.paper_number} className="rounded-lg border border-slate-200 p-4">
                <div className="flex items-center justify-between">
                  <p className="font-medium text-slate-900">
                    Paper {spec.paper_number}{' '}
                    <span className="text-sm font-normal text-slate-500">(Day {spec.day})</span>
                  </p>
                  <Badge tone={spec.can_assemble ? 'green' : 'amber'}>
                    {spec.can_assemble ? 'Ready' : 'Short'}
                  </Badge>
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  {spec.seq_required} SEQ + {spec.vsaq_required} VSAQ · {spec.total_marks} marks ·{' '}
                  {spec.writing_minutes} min writing
                </p>
                <div className="mt-3 flex gap-2">
                  <Button
                    size="sm"
                    disabled={!spec.can_assemble}
                    loading={busy === spec.paper_number}
                    onClick={() => assemble(spec.paper_number, false)}
                  >
                    Assemble
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    loading={busy === spec.paper_number}
                    onClick={() => assemble(spec.paper_number, true)}
                  >
                    Assemble anyway
                  </Button>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card title="Assembled papers">
        {papers.length === 0 ? (
          <EmptyState title="No papers yet" />
        ) : (
          <ul className="divide-y divide-slate-100">
            {papers.map((paper) => (
              <li key={paper.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                <div>
                  <div className="flex items-center gap-2">
                    <p className="font-medium text-slate-800">{paper.title}</p>
                    <Badge tone={paper.is_published ? 'green' : 'slate'}>
                      {paper.is_published ? 'Published' : 'Draft'}
                    </Badge>
                  </div>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {paper.question_count} questions · {paper.total_marks} marks
                    {paper.cut_score != null && ` · cut score ${paper.cut_score}`}
                  </p>
                </div>
                <div className="flex gap-1">
                  <Button variant="ghost" size="sm" onClick={() => togglePublish(paper)}>
                    {paper.is_published ? 'Unpublish' : 'Publish'}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => remove(paper)}>
                    Delete
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
