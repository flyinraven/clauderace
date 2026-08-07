import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import { useSpeech } from '../hooks/useSpeech'
import { Alert, Button, Card, Loading } from '../components/ui'

/**
 * The two minutes between stations on a real circuit.
 *
 * The candidate walks to the next door and waits for the bell. Here the clock
 * counts down and then moves them on by itself - but a candidate who is ready
 * may start early, which is why the button is the prominent thing on screen
 * and the countdown is only a number beside it.
 *
 * Nothing about the station just finished is shown. Marking runs behind this
 * screen and the result is held until the whole circuit is done: seeing
 * station three's mark before sitting station four changes how four is
 * answered, and that is not the exam.
 */
export default function OsceCircuitRest() {
  const { circuitId } = useParams()
  const [params] = useSearchParams()
  const navigate = useNavigate()

  const nextStationId = Number(params.get('next'))
  const position = Number(params.get('position') || 0)
  const stations = Number(params.get('stations') || 0)
  const [remaining, setRemaining] = useState(Number(params.get('rest') || 120))
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const speech = useSpeech()

  const startNext = useCallback(async () => {
    if (starting || !nextStationId) return
    // Before any await, while a tap on the button still counts as a gesture.
    // The station this leads to starts itself, so this is the last gesture
    // WebKit will see - without it the examiner's voice is silently dropped
    // for the rest of the circuit. Harmless when the countdown got here on
    // its own: synthesis was already unlocked by station one, in this same
    // document, and the SPA never reloads it.
    speech.unlock()
    setStarting(true)
    try {
      const sitting = await api<{ id: number }>('/osce/sittings', {
        method: 'POST',
        body: {
          station_id: nextStationId,
          circuit_id: Number(circuitId),
          is_timed: true,
        },
      })
      // `autostart` says the candidate has already committed to starting -
      // they pressed the button on this screen, or let the rest run out. Asking
      // them again on the next screen is a second gate on one decision.
      navigate(`/osce/sittings/${sitting.id}?autostart=1`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start the next station')
      setStarting(false)
    }
  }, [circuitId, navigate, nextStationId, speech, starting])

  useEffect(() => {
    if (remaining <= 0) {
      void startNext()
      return
    }
    const timer = window.setTimeout(() => setRemaining((s) => s - 1), 1000)
    return () => window.clearTimeout(timer)
  }, [remaining, startNext])

  if (!nextStationId) return <Loading />

  const minutes = Math.floor(Math.max(0, remaining) / 60)
  const seconds = Math.max(0, remaining) % 60

  return (
    <div className="mx-auto max-w-xl px-4 py-16">
      <Card title="Rest between stations">
        {error && <Alert tone="error">{error}</Alert>}
        <p className="text-sm text-slate-600">
          {stations > 0
            ? `Station ${position} of ${stations} is done. The next one starts automatically.`
            : 'The next station starts automatically.'}
        </p>
        <p className="mt-6 text-center font-mono text-5xl tabular-nums text-slate-900">
          {minutes}:{String(seconds).padStart(2, '0')}
        </p>
        <div className="mt-8 flex justify-center">
          <Button onClick={startNext} loading={starting}>
            Start the next station now
          </Button>
        </div>
        <p className="mt-6 text-center text-xs text-slate-500">
          Your answers are being marked in the background. You will see every result
          together once the circuit is finished.
        </p>
      </Card>
    </div>
  )
}
