import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { useJob } from '../hooks/useJob'
import { Alert, Badge, Button, Card, EmptyState, Loading, ProgressBar } from '../components/ui'

interface Station {
  id: number
  station_number: number | null
  subspecialty: string | null
  title: string | null
  case_summary: string | null
  exam_period: string | null
  source: string | null
  total_marks: number
  prompt_count: number
  prompts_status: string
  attempted: boolean
  attempt_count: number
  last_attempt_at: string | null
}

interface Circuit {
  id: number
  title: string
  scheduled_for: string | null
  station_ids: number[]
  status: string
  progress: {
    stations: number
    completed: number
    total_awarded: number
    total_available: number
    percentage: number | null
  }
}

export default function Osce() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [stations, setStations] = useState<Station[]>([])
  const [circuits, setCircuits] = useState<Circuit[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [prepJob, setPrepJob] = useState<number | null>(null)

  const { job } = useJob(prepJob)

  const load = useCallback(() => {
    Promise.all([api<Station[]>('/osce/stations'), api<Circuit[]>('/osce/circuits')])
      .then(([s, c]) => {
        setStations(s)
        setCircuits(c)
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(load, [load])

  useEffect(() => {
    if (job?.status === 'completed') load()
  }, [job?.status, load])

  const startCircuit = async () => {
    setBusy(true)
    setError(null)
    try {
      const circuit = await api<Circuit>('/osce/circuits', {
        method: 'POST',
        body: { station_count: 9 },
      })
      load()
      const first = circuit.station_ids[0]
      if (first) await startStation(first, circuit.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not build a circuit')
    } finally {
      setBusy(false)
    }
  }

  const startStation = async (stationId: number, circuitId?: number) => {
    try {
      const sitting = await api<{ id: number }>('/osce/sittings', {
        method: 'POST',
        body: { station_id: stationId, circuit_id: circuitId ?? null, is_timed: true },
      })
      navigate(`/osce/sittings/${sitting.id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start the station')
    }
  }

  /** Forget my attempts at a station, putting it back in the circuit pool. */
  const clearAttempts = async (station: Station) => {
    const label = station.title ?? `Station ${station.station_number ?? station.id}`
    const n = station.attempt_count
    if (!confirm(`Clear ${n} attempt${n === 1 ? '' : 's'} at "${label}"?\n\nThe sitting and its marking are deleted, and circuits can pick this station again.`)) {
      return
    }
    try {
      await api(`/osce/stations/${station.id}/attempts`, { method: 'DELETE' })
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not clear the attempts')
    }
  }

  const prepareStations = async () => {
    try {
      const result = await api<{ job_id: number }>('/osce/stations/build-prompts', {
        method: 'POST',
      })
      setPrepJob(result.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Nothing to prepare')
    }
  }

  if (loading) return <Loading label="Loading stations…" />

  const ready = stations.filter((s) => s.prompts_status === 'complete')
  const notReady = stations.length - ready.length
  const unattempted = ready.filter((s) => !s.attempted)

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">OSCE practice</h1>
          <p className="mt-1 text-sm text-slate-500">
            Spoken stations, 9 minutes each. You answer aloud; two examiners mark the
            transcript against the station rubric.
          </p>
        </div>
        <div className="flex gap-2">
          {user?.role === 'admin' && notReady > 0 && (
            <Button variant="secondary" onClick={prepareStations}>
              Prepare {notReady} station(s)
            </Button>
          )}
          <Button onClick={startCircuit} loading={busy} disabled={ready.length === 0}>
            Start today's circuit
          </Button>
        </div>
      </div>

      {error && <Alert tone="error">{error}</Alert>}

      {job && ['pending', 'running'].includes(job.status) && (
        <Card title="Preparing stations">
          <ProgressBar value={job.progress} label={job.message ?? undefined} />
        </Card>
      )}

      {ready.length === 0 && (
        <Alert tone="warning" title="No stations are ready yet">
          Upload an OSCE examiners' report under Documents, then press “Prepare stations”
          to turn each one into a timed examiner conversation.
        </Alert>
      )}

      {ready.length > 0 && unattempted.length < 9 && (
        <Alert tone="info">
          {unattempted.length} station(s) you haven't sat remain, so the next circuit will be
          shorter than nine. Circuits never repeat a station you've sat — clear a station's
          attempts below to sit it again, or add more stations.
        </Alert>
      )}

      {circuits.length > 0 && (
        <Card title="Your circuits">
          <ul className="divide-y divide-slate-100">
            {circuits.slice(0, 8).map((circuit) => (
              <li key={circuit.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                <div>
                  <p className="font-medium text-slate-800">{circuit.title}</p>
                  <p className="text-xs text-slate-500">
                    {circuit.progress.completed} of {circuit.progress.stations} stations done
                    {circuit.progress.percentage != null &&
                      ` · ${circuit.progress.total_awarded}/${circuit.progress.total_available} marks (${circuit.progress.percentage}%)`}
                  </p>
                </div>
                <ProgressBar
                  value={
                    circuit.progress.stations
                      ? circuit.progress.completed / circuit.progress.stations
                      : 0
                  }
                />
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card
        title="All stations"
        description={`${ready.length} ready${notReady ? `, ${notReady} awaiting preparation` : ''}`}
      >
        {stations.length === 0 ? (
          <EmptyState title="No stations ingested yet" />
        ) : (
          <ul className="divide-y divide-slate-100">
            {stations.map((station) => (
              <li key={station.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                <div className="min-w-0 flex-1">
                  <p className="font-medium text-slate-800">
                    {station.title ?? station.case_summary?.slice(0, 70) ?? `Station ${station.station_number}`}
                  </p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {station.subspecialty ?? 'Unclassified'}
                    {station.exam_period && ` · ${station.exam_period}`}
                    {station.prompt_count > 0 && ` · ${station.prompt_count} questions`}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  {/* Past-paper stations carry the sitting they came from;
                      generated ones never do. Both are worth practising, but
                      only one is what the examiners actually asked. */}
                  {station.source === 'past_paper' ? (
                    <Badge tone="green">Past paper</Badge>
                  ) : (
                    <Badge tone="violet">Generated</Badge>
                  )}
                  {/* Circuits skip attempted stations, so the count is not
                      trivia - it is the reason a station stopped appearing,
                      and clearing it is how you ask for it back. */}
                  {station.attempted && (
                    <>
                      <Badge tone="slate">
                        {station.attempt_count > 1
                          ? `Sat ${station.attempt_count}×`
                          : 'Sat'}
                      </Badge>
                      <Button
                        size="sm"
                        variant="ghost"
                        title="Forget my attempts so circuits can pick this station again"
                        onClick={() => clearAttempts(station)}
                      >
                        Clear
                      </Button>
                    </>
                  )}
                  {station.prompts_status !== 'complete' ? (
                    <Badge tone="amber">Not ready</Badge>
                  ) : (
                    <Button size="sm" variant="secondary" onClick={() => startStation(station.id)}>
                      Practise
                    </Button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
