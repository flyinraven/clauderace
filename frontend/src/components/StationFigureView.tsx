import { useState } from 'react'
import { useImage } from '../hooks/useImage'

export interface StationFigure {
  id: number
  image_id: number | null
  caption: string | null
  // What the examiner states aloud when no photograph of this view exists.
  // Some signs are dynamic — fatiguable ptosis, Cogan's lid twitch — and no
  // still image can carry them at all.
  described_findings: string | null
  position: number
}

/**
 * One station figure, as the candidate may see it.
 *
 * Shared by the sitting and the review deliberately. The review used to show
 * no images at all, so a station whose question turned on a picture was
 * reviewed without it — the mark was there and the thing it was awarded for
 * was not. Reviewing against a different picture from the one on screen during
 * the station would be a worse version of the same fault, and one copy of this
 * cannot drift from the other.
 *
 * The backend decides what may be shown (`visible_figure`); this only draws it.
 */
export function StationFigureView({ figure }: { figure: StationFigure }) {
  const { url } = useImage(figure.image_id)
  const [zoomed, setZoomed] = useState(false)

  // What the examiner states aloud: either the whole view, when no image was
  // found at all, or just the signs the image on screen cannot show. Both are
  // marks the candidate was asked for.
  const spoken = figure.described_findings ? (
    <div className="border-t border-slate-200 bg-slate-50 px-3 py-2">
      <p className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
        On examination
      </p>
      <p className="mt-0.5 text-sm text-slate-700">{figure.described_findings}</p>
    </div>
  ) : null

  // Nothing was found for this view. Rendering the image frame would sit on
  // "Loading image…" for ever.
  if (!figure.image_id) {
    return <div className="rounded-lg border border-slate-200">{spoken}</div>
  }

  return (
    <figure className="overflow-hidden rounded-lg border border-slate-200">
      {url ? (
        <button
          type="button"
          className="block w-full cursor-zoom-in"
          onClick={() => setZoomed(true)}
        >
          <img src={url} alt={figure.caption ?? 'Clinical image'} className="w-full" />
        </button>
      ) : (
        <div className="flex h-44 items-center justify-center bg-slate-50 text-xs text-slate-400">
          Loading image…
        </div>
      )}
      {figure.caption && (
        <figcaption className="border-t border-slate-200 px-3 py-1.5 text-xs text-slate-600">
          {figure.caption}
        </figcaption>
      )}
      {spoken}
      {zoomed && url && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/85 p-4"
          onClick={() => setZoomed(false)}
          role="presentation"
        >
          <img src={url} alt="" className="max-h-full max-w-full rounded-lg" />
        </div>
      )}
    </figure>
  )
}
