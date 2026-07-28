import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, api } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { useJob } from '../hooks/useJob'
import { useImage } from '../hooks/useImage'
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
  findings_split_status: string
  has_given_findings: boolean
  attempted: boolean
  attempt_count: number
  last_attempt_at: string | null
}

interface RubricPoint {
  text: string
  marks: number
  is_critical: boolean
}

interface PreviewPrompt {
  label: string
  text: string
  seconds: number | null
  marks: number
  rubric: RubricPoint[]
}

interface PreviewFigure {
  id: number
  image_id: number | null
  caption: string | null
  is_approved: boolean
  verification_status: string
}

/** The station's clinical image, as the candidate would be shown it. */
function PreviewFigureView({ figure }: { figure: PreviewFigure }) {
  const { url, error } = useImage(figure.image_id)
  return (
    <figure className="overflow-hidden rounded-md border border-slate-200 bg-white">
      {url ? (
        <img src={url} alt={figure.caption ?? 'Clinical image'} className="w-full" />
      ) : (
        <div className="p-4 text-xs text-slate-500">{error ?? 'Loading image…'}</div>
      )}
      <figcaption className="border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
        {figure.caption ?? 'No caption'}
        {!figure.is_approved && (
          <span className="ml-2 font-medium text-amber-700">
            not approved ({figure.verification_status}) — the candidate will not see this
          </span>
        )}
      </figcaption>
    </figure>
  )
}

interface StationPreview {
  id: number
  title: string | null
  subspecialty: string | null
  patient_demographic: string | null
  findings_given: string | null
  findings_elicited: string | null
  diagnosis: string | null
  total_marks: number
  prompts: PreviewPrompt[]
  figures: PreviewFigure[]
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
  // Reviewing what a station asks should not cost nine minutes of sitting it.
  const [openId, setOpenId] = useState<number | null>(null)
  const [preview, setPreview] = useState<StationPreview | null>(null)

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

  /** Delete a station outright — for one that ingested badly. */
  const removeStation = async (station: Station) => {
    const label = station.title ?? `Station ${station.station_number ?? station.id}`
    if (!confirm(`Delete "${label}"?\n\nIts questions, marking scheme and images go with it. This cannot be undone.`)) {
      return
    }
    setError(null)
    try {
      await api(`/osce/stations/${station.id}`, { method: 'DELETE' })
      load()
    } catch (err) {
      // Refused while candidates have sat it, since their answers and marks
      // would go too. Confirming a second time is what consents to that.
      if (err instanceof ApiError && err.status === 409) {
        if (!confirm(`${err.message}\n\nDelete the station and those sittings?`)) return
        try {
          await api(`/osce/stations/${station.id}?delete_sittings=true`, { method: 'DELETE' })
          load()
          return
        } catch (forced) {
          setError(forced instanceof Error ? forced.message : 'Could not delete the station')
          return
        }
      }
      setError(err instanceof Error ? err.message : 'Could not delete the station')
    }
  }

  /** Wipe every attempt, for when a run of tests has hidden real stations. */
  const clearAllAttempts = async () => {
    const total = stations.reduce((sum, s) => sum + s.attempt_count, 0)
    if (!confirm(`Clear all ${total} attempt(s) across ${attemptedCount} station(s)?\n\nEvery sitting and its marking is deleted, and all stations return to the circuit pool.`)) {
      return
    }
    try {
      await api('/osce/attempts', { method: 'DELETE' })
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not clear the attempts')
    }
  }

  const togglePreview = async (stationId: number) => {
    if (openId === stationId) {
      setOpenId(null)
      setPreview(null)
      return
    }
    setOpenId(stationId)
    setPreview(null)
    try {
      setPreview(await api<StationPreview>(`/osce/stations/${stationId}/preview`))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load the station')
    }
  }

  const prepareStations = async (force = false) => {
    if (force && !confirm(`Rebuild the examiner questions for all ${stations.length} station(s)?

Every station is rewritten from its rubric, so existing stations pick up the standing opening instruction. Marking already recorded is untouched, but it was given against the old wording.`)) {
      return
    }
    try {
      const result = await api<{ job_id: number }>(`/osce/stations/build-prompts${force ? '?force=true' : ''}`, {
        method: 'POST',
      })
      setPrepJob(result.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Nothing to prepare')
    }
  }

  /**
   * Separate the numbers the examiner reads out (acuity, pressure) from the
   * signs the candidate has to find. Without it a station opens with the
   * demographic alone, which is not how a real station is set up.
   */
  const splitFindings = async () => {
    try {
      const result = await api<{ job_id: number }>('/osce/stations/split-findings', {
        method: 'POST',
      })
      setPrepJob(result.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Nothing to split')
    }
  }

  if (loading) return <Loading label="Loading stations…" />

  const ready = stations.filter((s) => s.prompts_status === 'complete')
  const notReady = stations.length - ready.length
  const unattempted = ready.filter((s) => !s.attempted)
  const attemptedCount = stations.filter((s) => s.attempted).length
  const needSplit = stations.filter((s) => ['none', 'failed'].includes(s.findings_split_status)).length

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
          {attemptedCount > 0 && (
            <Button variant="ghost" onClick={clearAllAttempts}>
              Reset {attemptedCount} sat station(s)
            </Button>
          )}
          {user?.role === 'admin' && needSplit > 0 && (
            <Button variant="secondary" onClick={splitFindings}>
              Split findings for {needSplit} station(s)
            </Button>
          )}
          {user?.role === 'admin' && notReady > 0 && (
            <Button variant="secondary" onClick={() => prepareStations()}>
              Prepare {notReady} station(s)
            </Button>
          )}
          {user?.role === 'admin' && stations.length > 0 && (
            <Button variant="ghost" onClick={() => prepareStations(true)}>
              Rebuild all questions
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
              <li key={station.id} className="py-3">
                <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <p className="font-medium text-slate-800">
                    {/* Never fall back to the case summary: it reads the case
                        out before the candidate has chosen to sit it. */}
                    {station.title ?? `Station ${station.station_number ?? station.id}`}
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
                  {user?.role === 'admin' && (
                    <>
                      <Button
                        size="sm"
                        variant="ghost"
                        title="Read the questions without sitting the station"
                        onClick={() => togglePreview(station.id)}
                      >
                        {openId === station.id ? 'Hide' : 'Review'}
                      </Button>
                      <button
                        type="button"
                        title="Delete this station outright"
                        onClick={() => removeStation(station)}
                        className="rounded-md px-2 py-1 text-xs font-medium text-slate-400 transition hover:bg-red-50 hover:text-red-600"
                      >
                        Delete
                      </button>
                    </>
                  )}
                </div>
                </div>

                {openId === station.id && (
                  <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-4">
                    {!preview ? (
                      <Loading label="Loading station…" />
                    ) : (
                      <div className="space-y-3 text-sm">
                        <p className="text-slate-600">
                          <span className="font-medium text-slate-800">Shown at the start:</span>{' '}
                          {preview.patient_demographic ?? '(no demographic)'}
                          {preview.findings_given ? ` — ${preview.findings_given}` : ' — no given findings'}
                        </p>
                        {preview.figures.length === 0 ? (
                          <p className="text-xs text-amber-700">
                            This station has no image — there is nothing for the candidate to look at.
                          </p>
                        ) : (
                          <div className="grid gap-3 sm:grid-cols-2">
                            {preview.figures.map((f) => (
                              <PreviewFigureView key={f.id} figure={f} />
                            ))}
                          </div>
                        )}
                        <ol className="space-y-3">
                          {preview.prompts.map((p) => (
                            <li key={p.label} className="rounded-md border border-slate-200 bg-white p-3">
                              <p className="font-medium text-slate-900">
                                {p.label}. {p.text}
                              </p>
                              <p className="mt-0.5 text-xs text-slate-500">
                                {p.marks} marks{p.seconds ? ` · ${p.seconds}s` : ''}
                              </p>
                              <ul className="mt-2 space-y-1 text-xs text-slate-600">
                                {p.rubric.map((pt, i) => (
                                  <li key={i}>
                                    • {pt.text} <span className="text-slate-400">({pt.marks})</span>
                                    {pt.is_critical && (
                                      <span className="ml-1 font-medium text-red-600">critical</span>
                                    )}
                                  </li>
                                ))}
                              </ul>
                            </li>
                          ))}
                        </ol>
                        {preview.diagnosis && (
                          <p className="text-slate-600">
                            <span className="font-medium text-slate-800">Diagnosis:</span>{' '}
                            {preview.diagnosis}
                          </p>
                        )}
                      </div>
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
