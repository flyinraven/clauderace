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

export function useSpeech() {
  const [supported, setSupported] = useState(false)
  const [enabled, setEnabled] = useState(
    () => (localStorage.getItem(ENABLED_KEY) ?? 'true') === 'true',
  )
  const [speaking, setSpeaking] = useState(false)
  const current = useRef<SpeechSynthesisUtterance | null>(null)

  useEffect(() => {
    setSupported(typeof window !== 'undefined' && 'speechSynthesis' in window)
  }, [])

  useEffect(() => {
    localStorage.setItem(ENABLED_KEY, String(enabled))
  }, [enabled])

  const cancel = useCallback(() => {
    if ('speechSynthesis' in window) window.speechSynthesis.cancel()
    current.current = null
    setSpeaking(false)
  }, [])

  /**
   * Speak `text`, resolving when it finishes. Must be called from a gesture
   * handler on iOS. Resolves immediately when disabled or unsupported, so
   * callers can always await it.
   */
  const speak = useCallback(
    (text: string): Promise<void> => {
      if (!enabled || !('speechSynthesis' in window) || !text.trim()) {
        return Promise.resolve()
      }
      window.speechSynthesis.cancel()

      return new Promise((resolve) => {
        const utterance = new SpeechSynthesisUtterance(text)
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

        utterance.onend = finish
        utterance.onerror = finish

        // Safety net: iOS occasionally never fires onend for a long utterance,
        // which would leave the station stuck before recording starts.
        const ceiling = Math.max(4000, text.length * 90)
        window.setTimeout(finish, ceiling)

        current.current = utterance
        setSpeaking(true)
        window.speechSynthesis.speak(utterance)
      })
    },
    [enabled],
  )

  useEffect(() => cancel, [cancel])

  return { supported, enabled, setEnabled, speaking, speak, cancel }
}
