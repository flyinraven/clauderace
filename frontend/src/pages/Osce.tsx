import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, api } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { useJob } from '../hooks/useJob'
import { useImage } from '../hooks/useImage'
import { Alert, Badge, Button, Card, EmptyState, Input, Loading, Pagination, ProgressBar, Select } from '../components/ui'

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

const STATIONS_PER_PAGE = 20

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
  const [search, setSearch] = useState('')
  const [fSubspecialty, setFSubspecialty] = useState('')
  const [fPeriod, setFPeriod] = useState('')
  // Which paper the next circuit comes from. Empty means a mixed circuit, one
  // station per subspecialty; a sitting means that paper in its own order,
  // nine at a time, so an eighteen-station paper takes two circuits.
  const [circuitPeriod, setCircuitPeriod] = useState('')
  const [fSource, setFSource] = useState('')
  const [fState, setFState] = useState('')
  // Deleting a bad sitting one station at a time is eighteen confirmations.
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [offset, setOffset] = useState(0)

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
        body: {
          station_count: 9,
          ...(circuitPeriod ? { exam_period: circuitPeriod } : {}),
        },
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

  /** One new station in every subspecialty - a fresh circuit's worth.
      Each arrives with its questions already in the examiner arc, and the
      images for them are sourced straight afterwards. */
  const generateStations = async () => {
    if (
      !confirm(
        `Generate 9 new stations, one per subspecialty?

Each is written by the AI in the examiner question format, then its images are searched for and verified. This spends credit and takes several minutes.`,
      )
    ) {
      return
    }
    try {
      const result = await api<{ job_id: number; total: number }>('/osce/stations/generate', {
        method: 'POST',
        body: { one_each: true },
      })
      setPrepJob(result.job_id)
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start the generation')
    }
  }

  /** Remove a circuit from the list. The sittings it ran are kept: a circuit
      is a plan for a day, not the record of the work done in it. */
  const removeCircuit = async (circuit: Circuit) => {
    if (
      !confirm(
        `Delete "${circuit.title}"?

The circuit goes; the stations you sat in it keep their answers and marks.`,
      )
    ) {
      return
    }
    try {
      await api(`/osce/circuits/${circuit.id}`, { method: 'DELETE' })
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not delete the circuit')
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

  /** Delete every selected station — a whole sitting that ingested badly. */
  const removeSelected = async () => {
    const ids = [...selected]
    if (ids.length === 0) return
    if (
      !confirm(
        `Delete ${ids.length} station(s)?\n\nTheir questions, marking schemes and images ` +
          'go with them. This cannot be undone.',
      )
    ) {
      return
    }
    setError(null)
    const failed: number[] = []
    for (const id of ids) {
      try {
        // Sittings are taken too: this is a bulk clear-out, and stopping to
        // ask per station would defeat the point of selecting them.
        await api(`/osce/stations/${id}?delete_sittings=true`, { method: 'DELETE' })
      } catch {
        failed.push(id)
      }
    }
    setSelected(new Set())
    load()
    if (failed.length) setError(`${failed.length} station(s) could not be deleted.`)
  }

  const toggleSelected = (id: number) => {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
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

  // Eighteen stations a sitting, several sittings ingested: finding the one
  // that came out wrong means being able to narrow the list.
  const subspecialties = Array.from(
    new Set(stations.map((s) => s.subspecialty).filter((s): s is string => !!s)),
  ).sort()
  const examPeriods = Array.from(
    new Set(stations.map((s) => s.exam_period).filter((s): s is string => !!s)),
  ).sort()

  const visible = stations.filter((station) => {
    if (fSubspecialty && station.subspecialty !== fSubspecialty) return false
    if (fPeriod && station.exam_period !== fPeriod) return false
    if (fSource && station.source !== fSource) return false
    if (fState === 'ready' && station.prompts_status !== 'complete') return false
    if (fState === 'not_ready' && station.prompts_status === 'complete') return false
    if (fState === 'attempted' && !station.attempted) return false
    if (fState === 'unattempted' && station.attempted) return false
    if (search.trim()) {
      const haystack = [
        station.title,
        station.subspecialty,
        station.case_summary,
        station.exam_period,
        `station ${station.station_number ?? ''}`,
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      if (!haystack.includes(search.trim().toLowerCase())) return false
    }
    return true
  })

  // A filter that narrows the list to fewer stations than the current page
  // starts at would otherwise show an empty page.
  const pageOffset = offset >= visible.length ? 0 : offset
  const paged = visible.slice(pageOffset, pageOffset + STATIONS_PER_PAGE)

  const ready = stations.filter((s) => s.prompts_status === 'complete')
  const notReady = stations.length - ready.length
  // The card's own summary describes what the filters left on screen, so it
  // has to count the visible stations rather than the whole library.
  const visibleReady = visible.filter((s) => s.prompts_status === 'complete').length
  const visibleNotReady = visible.length - visibleReady
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
          {user?.role === 'admin' && (
            <Button variant="secondary" onClick={generateStations}>
              Generate 9 new stations
            </Button>
          )}
          <Select
            value={circuitPeriod}
            onChange={(e) => setCircuitPeriod(e.target.value)}
            aria-label="Which paper to sit"
          >
            <option value="">Mixed circuit</option>
            {examPeriods.map((p) => (
              <option key={p} value={p}>Sit {p}</option>
            ))}
          </Select>
          <Button onClick={startCircuit} loading={busy} disabled={ready.length === 0}>
            {circuitPeriod ? `Start ${circuitPeriod}` : "Start today's circuit"}
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
                <div className="flex items-center gap-3">
                  <ProgressBar
                    value={
                      circuit.progress.stations
                        ? circuit.progress.completed / circuit.progress.stations
                        : 0
                    }
                  />
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => removeCircuit(circuit)}
                  >
                    Delete
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card
        title="All stations"
        description={`${visible.length} of ${stations.length} shown · ${visibleReady} ready${visibleNotReady ? `, ${visibleNotReady} awaiting preparation` : ''}`}
      >
        {stations.length === 0 ? (
          <EmptyState title="No stations ingested yet" />
        ) : (
          <>
          <div className="mb-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search title, case or number…"
            />
            <Select value={fSubspecialty} onChange={(e) => setFSubspecialty(e.target.value)}>
              <option value="">All subspecialties</option>
              {subspecialties.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </Select>
            <Select value={fPeriod} onChange={(e) => setFPeriod(e.target.value)}>
              <option value="">All sittings</option>
              {examPeriods.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </Select>
            <Select value={fSource} onChange={(e) => setFSource(e.target.value)}>
              <option value="">Past paper &amp; generated</option>
              <option value="past_paper">Past paper only</option>
              <option value="generated">Generated only</option>
            </Select>
            <Select value={fState} onChange={(e) => setFState(e.target.value)}>
              <option value="">Any state</option>
              <option value="ready">Ready</option>
              <option value="not_ready">Not ready</option>
              <option value="attempted">Sat</option>
              <option value="unattempted">Not sat</option>
            </Select>
          </div>
          {(search || fSubspecialty || fPeriod || fSource || fState) && (
            <div className="mb-3">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setSearch('')
                  setFSubspecialty('')
                  setFPeriod('')
                  setFSource('')
                  setFState('')
                }}
              >
                Clear filters
              </Button>
            </div>
          )}

          {user?.role === 'admin' && visible.length > 0 && (
            <div className="mb-2 flex flex-wrap items-center gap-2 border-b border-slate-100 pb-2">
              <label className="flex items-center gap-2 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={paged.length > 0 && paged.every((s) => selected.has(s.id))}
                  onChange={(e) =>
                    setSelected(e.target.checked ? new Set(paged.map((s) => s.id)) : new Set())
                  }
                  className="rounded border-slate-300"
                />
                Select all on this page
              </label>
              {selected.size > 0 && (
                <>
                  <span className="text-xs text-slate-500">{selected.size} selected</span>
                  <Button size="sm" variant="ghost" onClick={removeSelected}>
                    Delete selected
                  </Button>
                </>
              )}
            </div>
          )}

          {visible.length === 0 ? (
            <EmptyState title="No stations match these filters" />
          ) : (
          <ul className="divide-y divide-slate-100">
            {paged.map((station) => (
              <li key={station.id} className="py-3">
                <div className="flex flex-wrap items-center justify-between gap-3">
                {user?.role === 'admin' && (
                  <input
                    type="checkbox"
                    checked={selected.has(station.id)}
                    onChange={() => toggleSelected(station.id)}
                    className="rounded border-slate-300"
                    aria-label={`Select ${station.title ?? `station ${station.station_number ?? station.id}`}`}
                  />
                )}
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
                    {/* The id every log line, audit row and support answer uses.
                        Without it on screen there is no way to tell which
                        "Station 7" anyone means: the number restarts with each
                        paper, and several stations share a title. */}
                    <span className="ml-1 font-mono text-slate-400">#{station.id}</span>
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
          <Pagination
            total={visible.length}
            pageSize={STATIONS_PER_PAGE}
            offset={pageOffset}
            noun="station"
            onOffset={(next) => {
              setOffset(next)
              setSelected(new Set())
            }}
          />
          </>
        )}
      </Card>
    </div>
  )
}
