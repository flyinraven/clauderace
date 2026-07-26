import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { Job } from '../types'

const POLL_INTERVAL_MS = 2000

/**
 * Poll a background job until it finishes.
 *
 * The polling doubles as a keep-alive: Render's free instance sleeps after 15
 * minutes of inactivity, and a long ingestion run would otherwise be paused
 * whenever the operator leaves the tab idle.
 */
export function useJob(jobId: number | null) {
  const [job, setJob] = useState<Job | null>(null)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  const stop = useCallback(() => {
    if (timer.current !== null) {
      window.clearInterval(timer.current)
      timer.current = null
    }
  }, [])

  useEffect(() => {
    if (!jobId) {
      setJob(null)
      return
    }
    let cancelled = false

    const poll = async () => {
      try {
        const next = await api<Job>(`/jobs/${jobId}`)
        if (cancelled) return
        setJob(next)
        setError(null)
        if (['completed', 'failed', 'cancelled'].includes(next.status)) stop()
      } catch (err) {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Could not read job status')
      }
    }

    void poll()
    timer.current = window.setInterval(poll, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      stop()
    }
  }, [jobId, stop])

  const isRunning = job !== null && ['pending', 'running'].includes(job.status)
  return { job, error, isRunning }
}
