import { useEffect, useRef, useState } from 'react'
import { ApiError, api, warm } from '../../api/client'
import { useJob } from '../../hooks/useJob'
import { Alert, Badge, Button, Card, EmptyState, Field, Input, Loading, ProgressBar } from '../../components/ui'
import type { SourceDocument } from '../../types'

interface UploadResponse {
  document: SourceDocument
  job_id: number
  detected_kind: string
  detected_blocks: number
}

export default function Documents() {
  const [documents, setDocuments] = useState<SourceDocument[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [examPeriod, setExamPeriod] = useState('')
  const [activeJob, setActiveJob] = useState<number | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const { job, isRunning } = useJob(activeJob)

  const load = () => {
    api<SourceDocument[]>('/documents')
      .then(setDocuments)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  // This is the one page that POSTs a large file, and a free-tier instance
  // drops whatever wakes it. Wake it now, while the administrator is still
  // choosing a file, rather than with the upload itself.
  useEffect(() => {
    void warm()
  }, [])

  // Refresh the table when a job finishes so counts and statuses are current.
  useEffect(() => {
    if (job && ['completed', 'failed'].includes(job.status)) load()
  }, [job?.status])

  const upload = async (event: React.FormEvent) => {
    event.preventDefault()
    const file = fileRef.current?.files?.[0]
    if (!file) return

    setUploading(true)
    setError(null)
    setNotice(null)
    try {
      const form = new FormData()
      form.append('file', file)
      if (examPeriod.trim()) form.append('exam_period', examPeriod.trim())

      // Retrying is safe here and nowhere else: the server hashes the file and
      // refuses a repeat with 409, so a retry cannot ingest it twice.
      const result = await api<UploadResponse>('/documents', {
        method: 'POST',
        body: form,
        retry: true,
      })
      setNotice(
        `Detected a ${result.detected_kind === 'osce' ? 'clinical OSCE' : 'written'} report with ` +
          `${result.detected_blocks} item(s). Structuring has started.`,
      )
      setActiveJob(result.job_id)
      if (fileRef.current) fileRef.current.value = ''
      load()
    } catch (err) {
      // A 409 on a retry means the attempt before it did land, and the
      // connection died on the way back. The document is in - say so rather
      // than reporting a duplicate the administrator never made.
      if (err instanceof ApiError && err.status === 409 && err.afterRetry) {
        setNotice('Uploaded. The connection dropped before the server could confirm it.')
        if (fileRef.current) fileRef.current.value = ''
        load()
      } else {
        setError(err instanceof Error ? err.message : 'Upload failed')
      }
    } finally {
      setUploading(false)
    }
  }

  const reingest = async (id: number) => {
    setError(null)
    try {
      const result = await api<{ job_id: number }>(`/documents/${id}/reingest`, { method: 'POST' })
      setActiveJob(result.job_id)
      setNotice('Re-ingestion started.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start re-ingestion')
    }
  }

  const remove = async (doc: SourceDocument) => {
    // "item(s)" rather than "question(s)": an OSCE report yields stations.
    const extra = doc.question_count > 0 ? ` and its ${doc.question_count} extracted item(s)` : ''
    if (!confirm(`Delete "${doc.filename}"${extra}? This cannot be undone.`)) return
    try {
      await api(`/documents/${doc.id}?delete_questions=true`, { method: 'DELETE' })
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed')
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Documents</h1>
        <p className="mt-1 text-sm text-slate-500">
          Upload RANZCO examiners' reports or past papers. Questions, marks, figures and examiner
          feedback are extracted automatically.
        </p>
      </div>

      {error && <Alert tone="error">{error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}

      <Card title="Upload a report" description="PDF, Word, plain text or JSON — up to 40 MB.">
        <form onSubmit={upload} className="grid gap-4 sm:grid-cols-[2fr_1fr_auto] sm:items-end">
          <Field label="File">
            <input
              ref={fileRef}
              type="file"
              accept=".pdf,.docx,.txt,.json,.md"
              required
              className="mt-1 block w-full text-sm text-slate-600 file:mr-3 file:rounded-lg file:border-0 file:bg-clinical-50 file:px-4 file:py-2 file:text-sm file:font-medium file:text-clinical-700 hover:file:bg-clinical-100"
            />
          </Field>
          <Field label="Exam period" hint="e.g. 2026 Semester 1">
            <Input value={examPeriod} onChange={(e) => setExamPeriod(e.target.value)} placeholder="2026 Semester 1" />
          </Field>
          <Button type="submit" loading={uploading} className="sm:mb-0.5">
            Upload &amp; ingest
          </Button>
        </form>
      </Card>

      {job && (
        <Card title="Ingestion progress">
          <ProgressBar
            value={job.progress}
            label={
              job.message ??
              `${job.completed_steps} of ${job.total_steps || '?'} items processed`
            }
          />
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Badge tone={job.status === 'completed' ? 'green' : job.status === 'failed' ? 'red' : 'blue'}>
              {job.status}
            </Badge>
            {isRunning && (
              <span className="text-xs text-slate-500">
                Keep this tab open — polling keeps the server awake while the job runs.
              </span>
            )}
          </div>
          {job.error && (
            <div className="mt-3">
              <Alert tone="error">{job.error}</Alert>
            </div>
          )}
          {Array.isArray(job.result?.warnings) && (job.result.warnings as string[]).length > 0 && (
            <div className="mt-3">
              <Alert tone="warning" title="Transcription warnings">
                <ul className="mt-1 list-inside list-disc space-y-0.5">
                  {(job.result.warnings as string[]).slice(0, 12).map((warning, index) => (
                    <li key={index}>{warning}</li>
                  ))}
                </ul>
              </Alert>
            </div>
          )}
        </Card>
      )}

      <Card title="Uploaded documents">
        {loading ? (
          <Loading />
        ) : documents.length === 0 ? (
          <EmptyState title="No documents yet">
            Upload an examiners' report to populate the question bank.
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
                  <th className="pb-2 font-medium">File</th>
                  <th className="pb-2 font-medium">Type</th>
                  <th className="pb-2 font-medium">Period</th>
                  <th className="pb-2 text-right font-medium">Pages</th>
                  <th className="pb-2 text-right font-medium">Items</th>
                  <th className="pb-2 font-medium">Status</th>
                  <th className="pb-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {documents.map((doc) => (
                  <tr key={doc.id}>
                    <td className="py-3 pr-3">
                      <p className="font-medium text-slate-800">{doc.filename}</p>
                      {doc.status_detail && (
                        <p className="mt-0.5 text-xs text-slate-500">{doc.status_detail}</p>
                      )}
                    </td>
                    <td className="py-3 pr-3">
                      <Badge tone={doc.document_kind === 'osce' ? 'violet' : 'blue'}>
                        {doc.document_kind ?? 'unknown'}
                      </Badge>
                    </td>
                    <td className="py-3 pr-3 text-slate-600">{doc.exam_period ?? '—'}</td>
                    <td className="py-3 pr-3 text-right tabular-nums text-slate-600">{doc.page_count ?? '—'}</td>
                    <td className="py-3 pr-3 text-right tabular-nums text-slate-600">{doc.question_count}</td>
                    <td className="py-3 pr-3">
                      <Badge
                        tone={
                          doc.status === 'completed'
                            ? 'green'
                            : doc.status === 'failed'
                              ? 'red'
                              : doc.status === 'completed_with_errors'
                                ? 'amber'
                                : 'slate'
                        }
                      >
                        {doc.status.replace(/_/g, ' ')}
                      </Badge>
                    </td>
                    <td className="py-3 text-right whitespace-nowrap">
                      <Button variant="ghost" size="sm" onClick={() => reingest(doc.id)}>
                        Re-ingest
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => remove(doc)}>
                        Delete
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
