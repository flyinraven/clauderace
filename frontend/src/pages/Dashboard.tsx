import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { Alert, Badge, Card, Loading } from '../components/ui'
import type { AdminStats } from '../types'

export default function Dashboard() {
  const { user } = useAuth()
  const [stats, setStats] = useState<AdminStats | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (user?.role !== 'admin') return
    api<AdminStats>('/admin/stats')
      .then(setStats)
      .catch((err) => setError(err instanceof Error ? err.message : 'Could not load statistics'))
  }, [user?.role])

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">
          Welcome{user?.full_name ? `, ${user.full_name.split(' ')[0]}` : ''}
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          Four written papers over two days, plus an 18-station OSCE circuit.
        </p>
      </div>

      <Card title="Examination format" description="Timings replicate the real RACE exam exactly.">
        <div className="grid gap-4 sm:grid-cols-2">
          {[
            { day: 'Day 1', papers: ['Paper 1 — 5 SEQ + 15 VSAQ · 1 h 40 min', 'Paper 2 — 4 SEQ + 15 VSAQ · 1 h 20 min'] },
            { day: 'Day 2', papers: ['Paper 3 — 5 SEQ + 15 VSAQ · 1 h 40 min', 'Paper 4 — 4 SEQ + 15 VSAQ · 1 h 20 min'] },
          ].map((group) => (
            <div key={group.day} className="rounded-lg border border-slate-200 p-4">
              <p className="text-sm font-semibold text-slate-900">{group.day}</p>
              <ul className="mt-2 space-y-1 text-sm text-slate-600">
                {group.papers.map((paper) => (
                  <li key={paper}>{paper}</li>
                ))}
              </ul>
              <p className="mt-3 text-xs text-slate-500">
                Each paper: 5 min preparation, 15 min reading, then writing time.
                A 30 min supervised break separates the two papers.
              </p>
            </div>
          ))}
        </div>
        <div className="mt-4">
          <Alert tone="info" title="Ready to sit">
            Take a paper under exam conditions or untimed from{' '}
            <Link to="/exams" className="font-medium underline">
              mock examinations
            </Link>
            , practise spoken stations in the{' '}
            <Link to="/osce" className="font-medium underline">
              OSCE circuit
            </Link>
            , or browse the{' '}
            <Link to="/bank" className="font-medium underline">
              question bank
            </Link>{' '}
            and its model answers.
          </Alert>
        </div>
      </Card>

      {user?.role === 'admin' && (
        <>
          {error && <Alert tone="error">{error}</Alert>}
          {!stats && !error ? (
            <Loading label="Loading statistics…" />
          ) : (
            stats && <AdminOverview stats={stats} />
          )}
        </>
      )}
    </div>
  )
}

function AdminOverview({ stats }: { stats: AdminStats }) {
  const tiles = [
    { label: 'Questions', value: stats.questions_total },
    { label: 'With model answers', value: stats.with_model_answers },
    { label: 'Documents ingested', value: stats.documents },
    { label: 'Users', value: stats.users },
    { label: 'Active jobs', value: stats.active_jobs },
    { label: 'Errors (24 h)', value: stats.errors_24h },
  ]

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {tiles.map((tile) => (
          <div key={tile.label} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
            <p className="text-2xl font-semibold tabular-nums text-slate-900">{tile.value}</p>
            <p className="mt-0.5 text-xs text-slate-500">{tile.label}</p>
          </div>
        ))}
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Bank by subspecialty">
          {Object.keys(stats.questions_by_subspecialty).length === 0 ? (
            <p className="text-sm text-slate-500">No questions yet.</p>
          ) : (
            <ul className="space-y-2">
              {Object.entries(stats.questions_by_subspecialty)
                .sort((a, b) => b[1] - a[1])
                .map(([name, count]) => (
                  <li key={name} className="flex items-center justify-between text-sm">
                    <span className="text-slate-700">{name}</span>
                    <Badge>{count}</Badge>
                  </li>
                ))}
            </ul>
          )}
        </Card>

        <Card title="AI usage" description="Last 30 days across all tasks.">
          <dl className="grid grid-cols-2 gap-4 text-sm">
            <Stat label="Calls" value={stats.ai_last_30_days.calls.toLocaleString()} />
            <Stat label="Estimated cost" value={`US$${stats.ai_last_30_days.cost_usd.toFixed(2)}`} />
            <Stat label="Prompt tokens" value={stats.ai_last_30_days.prompt_tokens.toLocaleString()} />
            <Stat label="Output tokens" value={stats.ai_last_30_days.completion_tokens.toLocaleString()} />
          </dl>
          <p className="mt-3 text-xs text-slate-500">
            Cost is reported by the provider where available; OpenRouter supplies it per call.
          </p>
        </Card>
      </div>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-lg font-semibold tabular-nums text-slate-900">{value}</dd>
    </div>
  )
}
