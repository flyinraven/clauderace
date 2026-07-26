import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import { Alert, Badge, Button, Card, EmptyState, Input, Loading } from '../../components/ui'
import type { ErrorEntry } from '../../types'

export default function Errors() {
  const [entries, setEntries] = useState<ErrorEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const [expanded, setExpanded] = useState<number | null>(null)

  const load = (source?: string) => {
    setLoading(true)
    const query = source?.trim() ? `?source=${encodeURIComponent(source.trim())}` : ''
    api<ErrorEntry[]>(`/admin/errors${query}`)
      .then(setEntries)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => load(), [])

  const clear = async () => {
    if (!confirm('Delete all log entries?')) return
    await api('/admin/errors?keep=0', { method: 'DELETE' })
    load(filter)
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Error log</h1>
          <p className="mt-1 text-sm text-slate-500">
            Server-side failures, including per-question ingestion problems.
          </p>
        </div>
        <div className="flex items-end gap-2">
          <Input
            className="mt-0 w-56"
            placeholder="Filter by source…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && load(filter)}
          />
          <Button variant="secondary" size="sm" onClick={() => load(filter)}>
            Search
          </Button>
          <Button variant="ghost" size="sm" onClick={clear}>
            Clear
          </Button>
        </div>
      </div>

      {error && <Alert tone="error">{error}</Alert>}

      <Card>
        {loading ? (
          <Loading />
        ) : entries.length === 0 ? (
          <EmptyState title="Nothing logged">The system has recorded no errors.</EmptyState>
        ) : (
          <ul className="divide-y divide-slate-100">
            {entries.map((entry) => (
              <li key={entry.id} className="py-3">
                <button
                  type="button"
                  className="flex w-full items-start gap-3 text-left"
                  onClick={() => setExpanded(expanded === entry.id ? null : entry.id)}
                >
                  <Badge tone={entry.level === 'error' ? 'red' : 'amber'}>{entry.level}</Badge>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-slate-800">{entry.message}</p>
                    <p className="mt-0.5 text-xs text-slate-500">
                      {entry.source} · {new Date(entry.created_at).toLocaleString('en-AU')}
                    </p>
                  </div>
                </button>
                {expanded === entry.id && (
                  <div className="mt-2 space-y-2">
                    {entry.context && (
                      <pre className="overflow-x-auto rounded-lg bg-slate-50 p-3 text-xs text-slate-600">
                        {JSON.stringify(entry.context, null, 2)}
                      </pre>
                    )}
                    {entry.detail && (
                      <pre className="max-h-80 overflow-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-100">
                        {entry.detail}
                      </pre>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
