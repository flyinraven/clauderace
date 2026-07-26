import { useEffect, useState } from 'react'
import { fetchImageObjectUrl } from '../api/client'

/** Load an authenticated image and hand back an object URL for <img src>. */
export function useImage(imageId: number | null | undefined) {
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!imageId) {
      setUrl(null)
      return
    }
    let objectUrl: string | null = null
    let cancelled = false

    fetchImageObjectUrl(imageId)
      .then((next) => {
        if (cancelled) {
          URL.revokeObjectURL(next)
          return
        }
        objectUrl = next
        setUrl(next)
      })
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : 'Image failed to load'))

    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [imageId])

  return { url, error }
}
