import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'

export interface ClockState {
  phase: 'not_started' | 'prep' | 'reading' | 'writing' | 'submitted' | 'expired'
  server_time: string
  started_at: string | null
  phase_ends_at: string | null
  paper_ends_at: string | null
  seconds_remaining_in_phase: number
  seconds_remaining_total: number
  can_view_questions: boolean
  can_write_answers: boolean
  can_take_notes: boolean
}

/** How often to re-sync with the server. Between syncs the countdown is local. */
const RESYNC_MS = 20_000

/**
 * Exam countdown driven by the server.
 *
 * The server is the only authority on remaining time. Between syncs the
 * numbers tick down locally for a smooth display, but every sync snaps them
 * back to the truth — so a paused laptop, a clock change, or a Render cold
 * start cannot hand the candidate extra minutes.
 */
export function useExamClock(sessionId: number | null, enabled = true) {
  const [clock, setClock] = useState<ClockState | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Local countdown, reset to the server value on every sync.
  const [phaseLeft, setPhaseLeft] = useState(0)
  const [totalLeft, setTotalLeft] = useState(0)
  const syncing = useRef(false)

  const sync = useCallback(async () => {
    if (!sessionId || syncing.current) return
    syncing.current = true
    try {
      const next = await api<ClockState>(`/sessions/${sessionId}/clock`)
      setClock(next)
      setPhaseLeft(next.seconds_remaining_in_phase)
      setTotalLeft(next.seconds_remaining_total)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Lost contact with the server')
    } finally {
      syncing.current = false
    }
  }, [sessionId])

  useEffect(() => {
    if (!sessionId || !enabled) return
    void sync()
    const timer = window.setInterval(sync, RESYNC_MS)
    // A tab that was backgrounded may have had its timers throttled; re-sync
    // the moment it becomes visible again.
    const onVisible = () => document.visibilityState === 'visible' && void sync()
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [sessionId, enabled, sync])

  useEffect(() => {
    if (!clock || !['prep', 'reading', 'writing'].includes(clock.phase)) return
    const timer = window.setInterval(() => {
      setPhaseLeft((v) => Math.max(0, v - 1))
      setTotalLeft((v) => Math.max(0, v - 1))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [clock])

  // When a phase runs out locally, sync immediately so the transition is
  // driven by the server rather than by our estimate.
  useEffect(() => {
    if (phaseLeft === 0 && clock && ['prep', 'reading', 'writing'].includes(clock.phase)) {
      void sync()
    }
  }, [phaseLeft, clock, sync])

  return { clock, phaseLeft, totalLeft, error, resync: sync }
}

export function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds))
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`
}
