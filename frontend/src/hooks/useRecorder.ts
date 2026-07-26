import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Microphone recording that works on iOS Safari.
 *
 * Constraints this is built around:
 * - iPhone has no usable SpeechRecognition, so there is no live transcript.
 *   A level meter stands in, giving the candidate visible proof the mic is
 *   picking them up.
 * - iOS Safari's MediaRecorder only emits audio/mp4 (AAC); Chromium emits
 *   audio/webm (Opus). The supported type is probed rather than assumed.
 * - The mic stream is opened once and kept, because iOS prompts for permission
 *   on every fresh getUserMedia call and that would interrupt every question.
 */

const PREFERRED_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4',
  'audio/mp4;codecs=mp4a.40.2',
  'audio/aac',
]

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === 'undefined') return undefined
  for (const type of PREFERRED_TYPES) {
    if (MediaRecorder.isTypeSupported?.(type)) return type
  }
  return undefined
}

export interface Recording {
  blob: Blob
  mimeType: string
  durationMs: number
}

export function useRecorder() {
  const [supported, setSupported] = useState(true)
  const [permission, setPermission] = useState<'idle' | 'granted' | 'denied'>('idle')
  const [recording, setRecording] = useState(false)
  const [level, setLevel] = useState(0)
  const [error, setError] = useState<string | null>(null)

  const streamRef = useRef<MediaStream | null>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<BlobPart[]>([])
  const startedAtRef = useRef(0)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const rafRef = useRef<number | null>(null)

  useEffect(() => {
    const ok =
      typeof navigator !== 'undefined' &&
      !!navigator.mediaDevices?.getUserMedia &&
      typeof MediaRecorder !== 'undefined'
    setSupported(ok)
  }, [])

  const stopMeter = useCallback(() => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current)
    rafRef.current = null
    setLevel(0)
  }, [])

  /** Ask for the mic once, up front, so no question is interrupted by a prompt. */
  const requestAccess = useCallback(async () => {
    setError(null)
    if (streamRef.current) {
      setPermission('granted')
      return true
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })
      streamRef.current = stream
      setPermission('granted')

      // Level meter, so the candidate can see the mic is live.
      const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
      const ctx = new Ctx()
      audioCtxRef.current = ctx
      const source = ctx.createMediaStreamSource(stream)
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 512
      source.connect(analyser)
      const data = new Uint8Array(analyser.frequencyBinCount)

      const tick = () => {
        analyser.getByteTimeDomainData(data)
        let peak = 0
        for (let i = 0; i < data.length; i += 1) {
          peak = Math.max(peak, Math.abs(data[i] - 128) / 128)
        }
        setLevel(peak)
        rafRef.current = requestAnimationFrame(tick)
      }
      rafRef.current = requestAnimationFrame(tick)
      return true
    } catch (err) {
      setPermission('denied')
      setError(
        err instanceof Error && err.name === 'NotAllowedError'
          ? 'Microphone access was blocked. Allow it in your browser settings, then reload.'
          : 'Could not open the microphone.',
      )
      return false
    }
  }, [])

  const start = useCallback(async () => {
    setError(null)
    if (!streamRef.current) {
      const ok = await requestAccess()
      if (!ok) return false
    }
    if (recorderRef.current?.state === 'recording') return true

    // iOS suspends the AudioContext until a user gesture resumes it.
    if (audioCtxRef.current?.state === 'suspended') {
      await audioCtxRef.current.resume().catch(() => undefined)
    }

    try {
      const mimeType = pickMimeType()
      const recorder = new MediaRecorder(
        streamRef.current!,
        mimeType ? { mimeType } : undefined,
      )
      chunksRef.current = []
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data)
      }
      recorder.start()
      recorderRef.current = recorder
      startedAtRef.current = Date.now()
      setRecording(true)
      return true
    } catch {
      setError('This browser refused to start recording.')
      return false
    }
  }, [requestAccess])

  /**
   * Suspend capture while the examiner's voice is playing, so it never lands
   * in the candidate's answer. Pausing rather than stopping keeps the single
   * recording intact - a stop would end the take and split the answer in two.
   */
  const pause = useCallback(() => {
    if (recorderRef.current?.state === 'recording') {
      recorderRef.current.pause()
      setRecording(false)
    }
  }, [])

  const resume = useCallback(() => {
    if (recorderRef.current?.state === 'paused') {
      recorderRef.current.resume()
      setRecording(true)
    }
  }, [])

  const stop = useCallback(async (): Promise<Recording | null> => {
    const recorder = recorderRef.current
    if (!recorder || recorder.state === 'inactive') {
      setRecording(false)
      return null
    }
    return new Promise((resolve) => {
      recorder.onstop = () => {
        const mimeType = recorder.mimeType || 'audio/mp4'
        const blob = new Blob(chunksRef.current, { type: mimeType })
        chunksRef.current = []
        setRecording(false)
        resolve(
          blob.size > 0
            ? { blob, mimeType, durationMs: Date.now() - startedAtRef.current }
            : null,
        )
      }
      recorder.stop()
    })
  }, [])

  /** Release the mic. Call when leaving the station. */
  const release = useCallback(() => {
    stopMeter()
    // Not just 'recording' - a recorder paused for the examiner's voice is
    // still holding the mic.
    recorderRef.current?.state !== 'inactive' && recorderRef.current?.stop()
    recorderRef.current = null
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    void audioCtxRef.current?.close().catch(() => undefined)
    audioCtxRef.current = null
    setPermission('idle')
  }, [stopMeter])

  useEffect(() => release, [release])

  return {
    supported, permission, recording, level, error,
    requestAccess, start, pause, resume, stop, release,
  }
}
