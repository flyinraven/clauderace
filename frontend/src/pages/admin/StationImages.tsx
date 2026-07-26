import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../../api/client'
import { useImage } from '../../hooks/useImage'
import { useJob } from '../../hooks/useJob'
import { Alert, Badge, Button, Card, EmptyState, Loading, ProgressBar, cx } from '../../components/ui'

interface StationFigure {
  id: number
  station_id: number
  image_id: number | null
  caption: string | null
  search_query: string | null
  verification_status: string
  verification_notes: string | null
  match_confidence: number | null
  is_approved: boolean
}

interface Station {
  id: number
  station_number: number | null
  subspecialty: string | null
  title: string | null
  case_summary: string | null
  prompts_status: string
}

export default function StationImages() {
  const [figures, setFigures] = useState<StationFigure[]>([])
  const [stations, setStations] = useState<Record<number, Station>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [jobId, setJobId] = useState<number | null>(null)
  const [filter, setFilter] = useState<'all' | 'pending' | 'approved' | 'rejected'>('pending')

  const { job } = useJob(jobId)

  const load = useCallback(() => {
    Promise.all([api<StationFigure[]>('/osce/figures'), api<Station[]>('/osce/stations')])
      .then(([f, s]) => {
        setFigures(f)
        setStations(Object.fromEntries(s.map((x) => [x.id, x])))
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(load, [load])

  useEffect(() => {
    if (job?.status === 'completed') load()
  }, [job?.status, load])

  const sourceImages = async () => {
    setError(null)
    try {
      const result = await api<{ job_id: number }>('/osce/stations/source-images', {
        method: 'POST',
      })
      setJobId(result.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Nothing to source')
    }
  }

  const setApproved = async (figure: StationFigure, approved: boolean) => {
    await api(`/osce/figures/${figure.id}/approve?approved=${approved}`, { method: 'POST' })
    load()
  }

  const reject = async (figure: StationFigure) => {
    if (!confirm('Remove this image? The station will have no image until one is sourced again.'))
      return
    await api(`/osce/figures/${figure.id}/image`, { method: 'DELETE' })
    load()
  }

  const shown = useMemo(() => {
    if (filter === 'all') return figures
    if (filter === 'approved') return figures.filter((f) => f.is_approved)
    if (filter === 'rejected') return figures.filter((f) => !f.image_id)
    return figures.filter((f) => f.image_id && !f.is_approved)
  }, [figures, filter])

  if (loading) return <Loading label="Loading station images…" />

  const withImage = figures.filter((f) => f.image_id).length
  const approved = figures.filter((f) => f.is_approved).length

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Station images</h1>
          <p className="mt-1 text-sm text-slate-500">
            {withImage} sourced · {approved} approved · {figures.length - withImage} without an image
          </p>
        </div>
        <Button onClick={sourceImages}>Source missing images</Button>
      </div>

      {error && <Alert tone="error">{error}</Alert>}

      <Alert tone="warning" title="A machine passed these; you decide">
        Each image was checked by a vision model against the station's own signs, which
        reliably removes diagrams, veterinary photos and marketing images. It is far less
        reliable about whether the image shows <em>this</em> patient's specific sign. Nothing
        is shown to a candidate until you approve it.
      </Alert>

      {job && ['pending', 'running'].includes(job.status) && (
        <Card title="Sourcing images">
          <ProgressBar value={job.progress} label={job.message ?? undefined} />
        </Card>
      )}

      <div className="flex flex-wrap gap-1">
        {(['pending', 'approved', 'rejected', 'all'] as const).map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => setFilter(value)}
            className={cx(
              'rounded-lg px-3 py-1.5 text-sm font-medium capitalize transition',
              filter === value ? 'bg-clinical-600 text-white' : 'bg-slate-100 text-slate-600',
            )}
          >
            {value === 'rejected' ? 'no image' : value}
          </button>
        ))}
      </div>

      {shown.length === 0 ? (
        <EmptyState title="Nothing here" />
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {shown.map((figure) => (
            <FigureCard
              key={figure.id}
              figure={figure}
              station={stations[figure.station_id]}
              onApprove={() => setApproved(figure, true)}
              onUnapprove={() => setApproved(figure, false)}
              onReject={() => reject(figure)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function FigureCard({
  figure,
  station,
  onApprove,
  onUnapprove,
  onReject,
}: {
  figure: StationFigure
  station?: Station
  onApprove: () => void
  onUnapprove: () => void
  onReject: () => void
}) {
  const { url } = useImage(figure.image_id)
  const [zoomed, setZoomed] = useState(false)

  const confidence = figure.match_confidence ?? 0
  const marginal = confidence > 0 && confidence < 0.78

  return (
    <Card
      title={`Station ${station?.station_number ?? figure.station_id} — ${station?.subspecialty ?? ''}`}
      description={station?.case_summary?.slice(0, 110)}
      actions={
        <div className="flex gap-1.5">
          {figure.is_approved && <Badge tone="green">Approved</Badge>}
          {figure.verification_status === 'faithful' && <Badge tone="blue">Faithful</Badge>}
          {figure.verification_status === 'representative' && (
            <Badge tone="violet">Representative</Badge>
          )}
          {figure.match_confidence != null && (
            <Badge tone={marginal ? 'amber' : 'slate'}>
              {(confidence * 100).toFixed(0)}%
            </Badge>
          )}
        </div>
      }
    >
      {figure.image_id ? (
        <>
          {url ? (
            <button type="button" className="block w-full cursor-zoom-in" onClick={() => setZoomed(true)}>
              <img
                src={url}
                alt={figure.caption ?? 'Station image'}
                className="max-h-72 w-full rounded-lg object-contain"
              />
            </button>
          ) : (
            <div className="flex h-48 items-center justify-center rounded-lg bg-slate-50 text-xs text-slate-400">
              Loading…
            </div>
          )}

          {figure.verification_status === 'representative' && (
            <div className="mt-3">
              <Alert tone="warning" title="Representative, not this patient">
                Shows the right pathology but not every sign the station describes. Useful
                for recognising the disease; the rubric may ask for something this image
                cannot show, so read the note below before approving.
              </Alert>
            </div>
          )}

          {marginal && figure.verification_status === 'faithful' && (
            <div className="mt-3">
              <Alert tone="warning">
                Scraped past the threshold. Check this one especially carefully.
              </Alert>
            </div>
          )}

          <dl className="mt-3 space-y-1.5 text-xs">
            <div>
              <dt className="font-semibold text-slate-500">Caption shown to you in the station</dt>
              <dd className="text-slate-700">{figure.caption ?? '—'}</dd>
            </div>
            <div>
              <dt className="font-semibold text-slate-500">The model says it shows</dt>
              <dd className="text-slate-700">{figure.verification_notes ?? '—'}</dd>
            </div>
            <div>
              <dt className="font-semibold text-slate-500">Search query</dt>
              <dd className="font-mono text-slate-600">{figure.search_query ?? '—'}</dd>
            </div>
          </dl>

          <div className="mt-4 flex gap-2">
            {figure.is_approved ? (
              <Button size="sm" variant="secondary" onClick={onUnapprove}>
                Withdraw approval
              </Button>
            ) : (
              <Button size="sm" onClick={onApprove}>
                Approve for use
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={onReject}>
              Reject image
            </Button>
          </div>
        </>
      ) : (
        <>
          <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-slate-300 text-sm text-slate-500">
            No suitable image found
          </div>
          <dl className="mt-3 space-y-1.5 text-xs">
            <div>
              <dt className="font-semibold text-slate-500">Search query</dt>
              <dd className="font-mono text-slate-600">{figure.search_query ?? '—'}</dd>
            </div>
            <div>
              <dt className="font-semibold text-slate-500">Why every candidate was rejected</dt>
              <dd className="text-slate-700">{figure.verification_notes ?? '—'}</dd>
            </div>
          </dl>
        </>
      )}

      {zoomed && url && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/85 p-4"
          onClick={() => setZoomed(false)}
          role="presentation"
        >
          <img src={url} alt="" className="max-h-full max-w-full rounded-lg" />
        </div>
      )}
    </Card>
  )
}
