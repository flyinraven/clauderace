/**
 * Tell iOS how this page uses audio.
 *
 * The station holds the microphone open for the whole nine minutes. On iOS
 * that puts the page's audio session into record mode, and WebKit then routes
 * ALL output - including speech synthesis - to the earpiece receiver at call
 * volume, or drops it entirely. The examiner's questions were being spoken
 * correctly and simply never reaching the candidate.
 *
 * Safari 16.4+ exposes navigator.audioSession. Declaring "play-and-record"
 * explicitly keeps output on the loudspeaker while the mic is live, which is
 * exactly what a station needs. Older Safari ignores this, hence the guard.
 */

type SessionType = 'auto' | 'playback' | 'transient' | 'transient-solo' | 'play-and-record'

interface AudioSession {
  type: SessionType
}

function session(): AudioSession | undefined {
  if (typeof navigator === 'undefined') return undefined
  return (navigator as Navigator & { audioSession?: AudioSession }).audioSession
}

export function setAudioSession(type: SessionType) {
  const current = session()
  if (!current) return
  try {
    current.type = type
  } catch {
    /* Unsupported value on this Safari - leave the default alone. */
  }
}
