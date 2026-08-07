import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../../api/client'
import { useImage } from '../../hooks/useImage'
import { useJob } from '../../hooks/useJob'
import { Alert, Badge, Button, Card, EmptyState, Loading, ProgressBar, cx } from '../../components/ui'

interface StationFigure {
  id: number
  station_id: number
  image_id: number | null
  caption: string | null
  // What this figure is for. Several figures on one station are otherwise
  // indistinguishable: same heading, same case summary, different request.
  wanted_description: string | null
  search_query: string | null
  verification_status: string
  verification_notes: string | null
  match_confidence: number | null
  is_approved: boolean
  // Written when no image could answer the rubric. Held back until read: a
  // wrong image is obvious at a glance, a fluent wrong sentence is not.
  described_findings: string | null
  described_findings_approved: boolean
  rejection_count: number
}

interface Station {
  id: number
  station_number: number | null
  station_label: string | null
  subspecialty: string | null
  title: string | null
  case_summary: string | null
  exam_period: string | null
  prompts_status: string
}

/**
 * Only what the pipeline could not resolve on its own. Drives the tab and count.
 *
 * A flag has to mean something. Flagging every representative image, and
 * everything under a confidence the verifier itself accepted, put hundreds of
 * perfectly usable stations in the queue and made the queue worth ignoring. An
 * image that is live has already been through vision verification and, where it
 * misses a sign, has that sign written out beside it.
 *
 * What is left is the genuine unknown: the pipeline declined to publish it, so
 * it is asking. Everything else stays out of the way until it is rejected.
 */
function needsLook(figure: StationFigure): boolean {
  return Boolean(figure.image_id && !figure.is_approved)
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

  /** Bring every station into line with the protocol, without spending anything. */
  const settleStations = async () => {
    setError(null)
    try {
      const result = await api<{ job_id: number }>('/osce/stations/settle', { method: 'POST' })
      setJobId(result.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Nothing to settle')
    }
  }

  /** Make every question honest about the image that actually arrived. */
  const reconcileQuestions = async () => {
    setError(null)
    try {
      const result = await api<{ job_id: number }>('/osce/stations/reconcile-questions', {
        method: 'POST',
      })
      setJobId(result.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Nothing to reconcile')
    }
  }

  /** Re-grade the papers' own photographs under the current rule. */
  const recheckFigures = async () => {
    setError(null)
    try {
      const result = await api<{ job_id: number }>('/osce/stations/recheck-figures', {
        method: 'POST',
      })
      setJobId(result.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Nothing to recheck')
    }
  }

  /** The protocol's last resort, for the views sourcing could not fill. */
  const describeMissing = async () => {
    setError(null)
    try {
      const result = await api<{ job_id: number }>('/osce/figures/describe-missing', {
        method: 'POST',
      })
      setJobId(result.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Nothing to describe')
    }
  }

  const setApproved = async (figure: StationFigure, approved: boolean) => {
    await api(`/osce/figures/${figure.id}/approve?approved=${approved}`, { method: 'POST' })
    load()
  }

  const setDescriptionApproved = async (figure: StationFigure, approved: boolean) => {
    setError(null)
    try {
      await api(`/osce/figures/${figure.id}/approve-description?approved=${approved}`, {
        method: 'POST',
      })
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not update the description')
    }
  }

  // Rejecting remembers the source URL, so the replacement search cannot hand
  // back the same picture.
  const reject = async (figure: StationFigure, findReplacement: boolean) => {
    setError(null)
    try {
      const result = await api<{ job_id: number | null }>(
        `/osce/figures/${figure.id}/reject?find_replacement=${findReplacement}`,
        { method: 'POST' },
      )
      if (result.job_id) setJobId(result.job_id)
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not reject the image')
    }
  }

  /** Ask for one more view of a station that automatic coverage missed. */
  const addFigure = async (stationId: number) => {
    const wanted = prompt(
      'Describe the image this station still needs.\n\n' +
        'Write it as the view an examiner would show, e.g. "gonioscopy of the ' +
        'right angle showing the tube" or "external photograph of the left eye ' +
        'showing a recent penetrating keratoplasty".',
    )
    if (!wanted?.trim()) return
    setError(null)
    try {
      const result = await api<{ job_id: number | null }>(
        `/osce/stations/${stationId}/figures`,
        { method: 'POST', body: { wanted_description: wanted.trim(), source_now: true } },
      )
      if (result.job_id) setJobId(result.job_id)
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add the figure')
    }
  }

  /** Drop a figure the station should not have at all. */
  const removeFigure = async (figure: StationFigure) => {
    if (!confirm('Remove this figure from the station entirely?')) return
    setError(null)
    try {
      await api(`/osce/figures/${figure.id}`, { method: 'DELETE' })
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove the figure')
    }
  }

  const shown = useMemo(() => {
    if (filter === 'all') return figures
    if (filter === 'approved') return figures.filter((f) => f.is_approved)
    if (filter === 'rejected') return figures.filter((f) => !f.image_id)
    return figures.filter(needsLook)
  }, [figures, filter])

  if (loading) return <Loading label="Loading station images…" />

  const withImage = figures.filter((f) => f.image_id).length
  const approved = figures.filter((f) => f.is_approved).length
  const needsLookCount = figures.filter(needsLook).length

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Station images</h1>
          <p className="mt-1 text-sm text-slate-500">
            {withImage} sourced · {approved} live · {needsLookCount} worth a look ·{' '}
            {figures.length - withImage} without an image
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button onClick={sourceImages}>Source missing images</Button>
          <Button variant="secondary" onClick={recheckFigures}>
            Recheck paper images
          </Button>
          <Button variant="secondary" onClick={reconcileQuestions}>
            Match questions to images
          </Button>
          <Button variant="secondary" onClick={settleStations}>
            Settle stations
          </Button>
          <Button variant="secondary" onClick={describeMissing}>
            Describe the rest
          </Button>
        </div>
      </div>

      {error && <Alert tone="error">{error}</Alert>}

      <Alert tone="info" title="Images are live by default">
        Each was checked by a vision model against its station's own signs, which reliably
        removes diagrams, veterinary photos and marketing images — but is far less reliable
        about whether it shows <em>this</em> patient's particular sign. So they appear at
        their stations straight away and you reject the wrong ones. Where an image misses one
        of the station's signs, that sign is written out beside it, and the station goes live
        anyway. <strong>Worth a look</strong> holds only what could not be settled that way.{' '}
        <strong>Reject &amp; find another</strong> remembers what you turned down and goes
        looking again, so you never see the same picture twice.
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
              'rounded-lg px-3 py-1.5 text-sm font-medium transition',
              filter === value ? 'bg-clinical-600 text-white' : 'bg-slate-100 text-slate-600',
            )}
          >
            {{ pending: 'Needs a look', approved: 'Live', rejected: 'No image', all: 'All' }[value]}
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
              onApproveDescription={() => setDescriptionApproved(figure, true)}
              onWithdrawDescription={() => setDescriptionApproved(figure, false)}
              onRejectAndReplace={() => reject(figure, true)}
              onUploaded={load}
              onRejectOnly={() => reject(figure, false)}
              onAddAnother={() => addFigure(figure.station_id)}
              onRemove={() => removeFigure(figure)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

/** Attach an image by hand, for an investigation no search can find.
 *
 * A Hess chart, a forced duction test being performed, an A-scan printout:
 * some things are simply not on the open web, and until this existed a
 * question wanting one could not be fixed by anybody. */
function UploadImage({ figureId, onDone }: { figureId: number; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const input = useRef<HTMLInputElement>(null)

  const send = async (file: File) => {
    setBusy(true)
    setFailed(null)
    const form = new FormData()
    form.append('image', file)
    try {
      await api(`/osce/figures/${figureId}/image`, { method: 'POST', body: form })
      onDone()
    } catch (err) {
      setFailed(err instanceof Error ? err.message : 'That did not upload')
    } finally {
      setBusy(false)
      if (input.current) input.current.value = ''
    }
  }

  return (
    <>
      <input
        ref={input}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file) void send(file)
        }}
      />
      <Button size="sm" variant="ghost" loading={busy} onClick={() => input.current?.click()}>
        Upload an image
      </Button>
      {failed && <span className="text-xs text-red-700">{failed}</span>}
    </>
  )
}

function FigureCard({
  figure,
  station,
  onApprove,
  onUnapprove,
  onApproveDescription,
  onWithdrawDescription,
  onRejectAndReplace,
  onUploaded,
  onRejectOnly,
  onAddAnother,
  onRemove,
}: {
  figure: StationFigure
  station?: Station
  onApprove: () => void
  onUnapprove: () => void
  onApproveDescription: () => void
  onWithdrawDescription: () => void
  onRejectAndReplace: () => void
  onUploaded: () => void
  onRejectOnly: () => void
  onAddAnother: () => void
  onRemove: () => void
}) {
  const { url } = useImage(figure.image_id)
  const [zoomed, setZoomed] = useState(false)

  const confidence = figure.match_confidence ?? 0
  const marginal = confidence > 0 && confidence < 0.78

  return (
    <Card
      // Station numbers repeat across sittings, so the period is what makes
      // this heading identify one station rather than four.
      title={[
        station?.title || `Station ${station?.station_label ?? station?.station_number ?? figure.station_id}`,
        station?.subspecialty,
        station?.exam_period,
      ]
        .filter(Boolean)
        .join(' — ')}
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
            {figure.wanted_description && (
              <div>
                <dt className="font-semibold text-slate-500">This view is for</dt>
                <dd className="text-slate-700">{figure.wanted_description}</dd>
              </div>
            )}
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

          <div className="mt-4 flex flex-wrap gap-2">
            <UploadImage figureId={figure.id} onDone={onUploaded} />
            <Button size="sm" onClick={onRejectAndReplace}>
              Reject &amp; find another
            </Button>
            <Button size="sm" variant="secondary" onClick={onRejectOnly}>
              Reject, leave empty
            </Button>
            {figure.is_approved ? (
              <Button size="sm" variant="ghost" onClick={onUnapprove}>
                Hide from station
              </Button>
            ) : (
              <Button size="sm" variant="ghost" onClick={onApprove}>
                Show at station
              </Button>
            )}
            {/* A rubric marking both eyes needs a view of each; coverage
                groups them automatically, and this is the one it missed. */}
            <Button size="sm" variant="ghost" onClick={onAddAnother}>
              Add another view
            </Button>
            <Button size="sm" variant="ghost" onClick={onRemove}>
              Remove figure
            </Button>
          </div>
          {figure.rejection_count > 0 && (
            <p className="mt-2 text-xs text-slate-500">
              {figure.rejection_count} image(s) already rejected for this station — the
              search skips them.
            </p>
          )}
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
          <div className="mt-4 flex flex-wrap gap-2">
            <UploadImage figureId={figure.id} onDone={onUploaded} />
            <Button size="sm" variant="ghost" onClick={onAddAnother}>
              Describe what it needs
            </Button>
            <Button size="sm" variant="ghost" onClick={onRemove}>
              Remove figure
            </Button>
          </div>
        </>
      )}

      {/* Written by a model and clinically wrong often enough that it must
          be read before a candidate is examined on it. The first live run
          produced horizontal motility findings for a station about elevation,
          and a cover test with no movement for a squint.

          Outside the image branch on purpose: a figure that found no image is
          exactly the one whose stated findings are all the candidate gets. */}
      {figure.described_findings && (
        <div
          className={cx(
            'mt-3 rounded-lg border p-3',
            figure.described_findings_approved
              ? 'border-green-200 bg-green-50'
              : 'border-amber-300 bg-amber-50',
          )}
        >
          <p className="text-xs font-semibold text-slate-700">
            {figure.described_findings_approved
              ? 'Stated to the candidate'
              : 'Proposed wording — not shown to anyone yet'}
          </p>
          <p className="mt-1 text-sm text-slate-800">{figure.described_findings}</p>
          <p className="mt-2 text-xs text-slate-600">
            Read this against the station's own findings before releasing it. A wrong
            sentence here is a mark the candidate cannot earn.
          </p>
          <div className="mt-2 flex gap-1.5">
            {figure.described_findings_approved ? (
              <Button variant="secondary" size="sm" onClick={onWithdrawDescription}>
                Withdraw it
              </Button>
            ) : (
              <Button size="sm" onClick={onApproveDescription}>
                Use this wording
              </Button>
            )}
          </div>
        </div>
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
