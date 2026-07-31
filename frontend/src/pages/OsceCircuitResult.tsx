import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { Alert, Badge, Card, Loading } from '../components/ui'

interface CircuitStation {
  station_id: number
  sitting_id: number | null
  title: string | null
  subspecialty: string | null
  submitted: boolean
  grading_status: string
  awarded: number | null
  available: number | null
}

interface CircuitResults {
  circuit_id: number
  title: string
  stations: CircuitStation[]
  complete: boolean
  total_awarded: number
  total_available: number
  awaiting_marking: number[]
}

/**
 * Every station's mark, together, once the circuit has been sat.
 *
 * Marking runs while the candidate is still going, so some stations may land
 * after this page first opens - it polls until the last one is in rather than
 * showing a total that is quietly missing a station.
 */
export default function OsceCircuitResult() {
  const { circuitId } = useParams()
  const [data, setData] = useState<CircuitResults | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    let timer: number | undefined

    const load = async () => {
      try {
        const body = await api<CircuitResults>(`/osce/circuits/${circuitId}/results`)
        if (!live) return
        setData(body)
        if (body.awaiting_marking.length > 0) {
          timer = window.setTimeout(load, 5000)
        }
      } catch (err) {
        if (live) setError(err instanceof Error ? err.message : 'Could not load the results')
      }
    }

    void load()
    return () => {
      live = false
      if (timer) window.clearTimeout(timer)
    }
  }, [circuitId])

  if (error) return <Alert tone="error">{error}</Alert>
  if (!data) return <Loading />

  const percentage = data.total_available
    ? Math.round((data.total_awarded / data.total_available) * 100)
    : null

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="text-2xl font-semibold text-slate-900">{data.title}</h1>
      <p className="mt-1 text-sm text-slate-600">
        {data.complete
          ? 'Circuit finished.'
          : 'Circuit not yet finished — the stations you have sat are below.'}
      </p>

      <Card title="Total">
        <p className="text-3xl font-semibold text-slate-900">
          {data.total_awarded}
          <span className="text-lg font-normal text-slate-500"> / {data.total_available}</span>
          {percentage != null && (
            <span className="ml-3 text-lg font-normal text-slate-500">{percentage}%</span>
          )}
        </p>
        {data.awaiting_marking.length > 0 && (
          <p className="mt-2 text-sm text-amber-700">
            {data.awaiting_marking.length} station(s) still being marked — this updates itself.
          </p>
        )}
      </Card>

      <Card title="Stations">
        <ul className="divide-y divide-slate-100">
          {data.stations.map((station, index) => (
            <li
              key={station.station_id}
              className="flex flex-wrap items-center justify-between gap-3 py-3"
            >
              <div>
                <p className="font-medium text-slate-800">
                  {index + 1}. {station.title ?? `Station ${station.station_id}`}
                </p>
                <p className="text-xs text-slate-500">
                  {station.subspecialty ?? 'Unclassified'}
                  <span className="ml-1 font-mono text-slate-400">#{station.station_id}</span>
                </p>
              </div>
              <div className="flex items-center gap-3">
                {station.awarded != null ? (
                  <span className="font-mono text-sm text-slate-800">
                    {station.awarded} / {station.available}
                  </span>
                ) : station.submitted ? (
                  <Badge tone="amber">Marking…</Badge>
                ) : (
                  <Badge>Not sat</Badge>
                )}
                {station.sitting_id && station.awarded != null && (
                  <Link
                    className="text-sm text-blue-700 hover:underline"
                    to={`/osce/sittings/${station.sitting_id}/result`}
                  >
                    Feedback
                  </Link>
                )}
              </div>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  )
}
