import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Read examiner questions aloud, as they are in the real OSCE.
 *
 * Uses the browser's built-in speech synthesis, so this costs nothing and
 * needs no API. Unlike SpeechRecognition, synthesis genuinely works on iOS
 * Safari - but with two constraints that shape the design here:
 *
 *   - speak() only fires inside a user gesture handler. WebKit silently drops
 *     the utterance otherwise, so every call must originate from a tap.
 *   - getVoices() returns nothing on Safari, so the voice cannot be chosen and
 *     the system default is used.
 */

const ENABLED_KEY = 'race.readAloud'
// The sound test is about the device, not the station, so once it has passed
// there is nothing to learn from repeating it at each station of a circuit.
const TESTED_KEY = 'race.soundTested'

export function useSpeech() {
  const [supported, setSupported] = useState(false)
  const [enabled, setEnabled] = useState(
    () => (localStorage.getItem(ENABLED_KEY) ?? 'true') === 'true',
  )
  const [soundTested, setSoundTested] = useState(
    () => localStorage.getItem(TESTED_KEY) === 'true',
  )
  const [speaking, setSpeaking] = useState(false)
  const [everSpoke, setEverSpoke] = useState(false)
  const current = useRef<SpeechSynthesisUtterance | null>(null)
  const unlocked = useRef(false)

  useEffect(() => {
    setSupported(typeof window !== 'undefined' && 'speechSynthesis' in window)
  }, [])

  useEffect(() => {
    localStorage.setItem(ENABLED_KEY, String(enabled))
  }, [enabled])

  const markSoundTested = useCallback(() => {
    localStorage.setItem(TESTED_KEY, 'true')
    setSoundTested(true)
  }, [])

  const cancel = useCallback(() => {
    if ('speechSynthesis' in window) window.speechSynthesis.cancel()
    current.current = null
    setSpeaking(false)
  }, [])

  /**
   * Unlock the speech engine. MUST be called synchronously inside a tap
   * handler, before any `await`.
   *
   * iOS only honours speak() when the call is still inside the gesture that
   * triggered it. Any intervening await - asking for the microphone, hitting
   * the API - ends that window, and WebKit then drops every later utterance
   * silently: the page thinks it is speaking and nothing is heard. Speaking a
   * single silent utterance while the gesture is still live unlocks synthesis
   * for the rest of the session, after which async calls work normally.
   */
  const unlock = useCallback(() => {
    if (unlocked.current || !('speechSynthesis' in window)) return
    try {
      const primer = new SpeechSynthesisUtterance(' ')
      // Not zero: a muted utterance is discarded by WebKit without ever
      // starting the engine, so it unlocks nothing. This is inaudible.
      primer.volume = 0.01
      window.speechSynthesis.speak(primer)
      unlocked.current = true
    } catch {
      /* nothing to do - speak() will simply stay silent */
    }
  }, [])

  /**
   * Speak `text`, resolving when it finishes. Call `unlock()` from the gesture
   * first. Resolves immediately when disabled or unsupported, so callers can
   * always await it.
   */
  const speak = useCallback(
    (text: string): Promise<void> => {
      if (!enabled || !('speechSynthesis' in window) || !text.trim()) {
        return Promise.resolve()
      }
      window.speechSynthesis.cancel()
      // iOS leaves the queue in a paused state after cancel(), and everything
      // spoken afterwards sits there silently. Harmless when not paused.
      window.speechSynthesis.resume()

      return new Promise((resolve) => {
        const utterance = new SpeechSynthesisUtterance(text)
        utterance.volume = 1
        // Slightly under normal pace: examiners read questions deliberately,
        // and clinical terms are easier to follow.
        utterance.rate = 0.95
        utterance.pitch = 1

        let settled = false
        const finish = () => {
          if (settled) return
          settled = true
          current.current = null
          setSpeaking(false)
          resolve()
        }

        // Only claim to be speaking once the engine actually starts. Setting
        // it optimistically made the UI report "Speaking..." while iOS had
        // silently dropped the utterance, which is worse than saying nothing.
        utterance.onstart = () => {
          setSpeaking(true)
          setEverSpoke(true)
        }
        utterance.onend = finish
        utterance.onerror = finish

        // Safety net: iOS sometimes never fires onend for a long utterance,
        // which would leave the station stuck before recording starts.
        //
        // It MUST silence the engine as well as resolve. Resolving alone let
        // the caller start recording while the question was still being read
        // aloud, and the microphone took it down as the candidate's answer -
        // one station came back with the examiner's question transcribed
        // verbatim ahead of the reply. The ceiling scales with the text, so it
        // is the longest questions, read for longest, that were exposed.
        const ceiling = Math.max(4000, text.length * 90)
        window.setTimeout(() => {
          if (settled) return
          window.speechSynthesis.cancel()
          finish()
        }, ceiling)

        current.current = utterance
        window.speechSynthesis.speak(utterance)
      })
    },
    [enabled],
  )

  useEffect(() => cancel, [cancel])

  return {
    supported, enabled, setEnabled, speaking, everSpoke, speak, unlock, cancel,
    soundTested, markSoundTested,
  }
}
