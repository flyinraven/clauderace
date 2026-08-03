/**
 * Re-encode a recording as 16 kHz mono WAV.
 *
 * MediaRecorder gives us whatever the browser feels like - webm/Opus on
 * Chromium, mp4/AAC on iOS Safari - and neither is a format the OpenAI-style
 * `input_audio` schema names, so those clips could only be transcribed through
 * Google's native API, whose free tier allows 20 requests a day: less than one
 * OSCE circuit. WAV is accepted by OpenRouter, so converting here keeps every
 * clip on the paid, unmetered route regardless of which browser recorded it.
 *
 * 16 kHz mono is what speech models resample to anyway, and it keeps a long
 * spoken answer to a few megabytes.
 */

const TARGET_RATE = 16000

function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)

  const writeString = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i))
  }

  writeString(0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  writeString(8, 'WAVE')
  writeString(12, 'fmt ')
  view.setUint32(16, 16, true) // PCM header size
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, 1, true) // mono
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true) // byte rate
  view.setUint16(32, 2, true) // block align
  view.setUint16(34, 16, true) // bits per sample
  writeString(36, 'data')
  view.setUint32(40, samples.length * 2, true)

  let offset = 44
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true)
    offset += 2
  }
  return new Blob([buffer], { type: 'audio/wav' })
}

/**
 * Decode `blob` and return it as WAV, or `null` if this browser cannot decode
 * its own recording - callers then fall back to uploading the original.
 */
export async function toWav(blob: Blob): Promise<Blob | null> {
  try {
    const Ctx =
      window.OfflineAudioContext ??
      (window as unknown as { webkitOfflineAudioContext: typeof OfflineAudioContext })
        .webkitOfflineAudioContext
    if (!Ctx) return null

    const bytes = await blob.arrayBuffer()

    // Decoding needs a live context; resampling to 16 kHz is done by rendering
    // through an offline context declared at the target rate.
    const DecodeCtx =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    const decodeCtx = new DecodeCtx()
    const decoded = await new Promise<AudioBuffer>((resolve, reject) => {
      // The callback form, because Safari's promise form is unreliable.
      decodeCtx.decodeAudioData(bytes.slice(0), resolve, reject)
    })
    void decodeCtx.close().catch(() => undefined)

    const frames = Math.max(1, Math.ceil((decoded.duration * TARGET_RATE) || 1))
    const offline = new Ctx(1, frames, TARGET_RATE)
    const source = offline.createBufferSource()
    source.buffer = decoded
    source.connect(offline.destination)
    source.start()
    const rendered = await offline.startRendering()

    return encodeWav(rendered.getChannelData(0), TARGET_RATE)
  } catch {
    return null
  }
}
