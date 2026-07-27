"""Normalise images before they are sent to a vision model.

A web-sourced clinical photograph is routinely 2500px wide and several megabytes.
Base64 inflates that by a third, so verifying one station's candidate images
uploaded tens of megabytes per station and made a provider timeout the most
likely outcome of a slow connection.

No provider looks at those pixels. Anthropic downsamples anything above roughly
1.15 megapixels before it counts tokens, and Gemini bills in 768px tiles, so
sending a 2500px original buys nothing over sending it at `MAX_EDGE` - the
tokens charged are the same and the upload is an order of magnitude smaller.

The transform is therefore deliberately conservative: shrink only what is above
the cap the provider would itself impose, and keep whichever encoding is
smaller. Anything Pillow cannot read is passed through untouched, because a
verification against the original beats no verification at all.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

# The longest edge any provider makes use of. Above this they downsample
# themselves, so anything larger is upload we pay for and they discard.
MAX_EDGE = 1568
JPEG_QUALITY = 85
# Re-encoding a small image is not worth the loss; below this, leave it alone.
MIN_BYTES_TO_BOTHER = 64 * 1024


def normalise_for_vision(data: bytes, media_type: str) -> tuple[bytes, str]:
    """Return (bytes, media_type) bounded to `MAX_EDGE`, or the input unchanged.

    Never raises: an image that cannot be decoded is returned as it came in.
    """
    if not data or len(data) < MIN_BYTES_TO_BOTHER:
        return data, media_type

    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(data)) as im:
            # An animated image loses its frames on re-encode; only the first
            # would be sent anyway, but leave the decision to the provider.
            if getattr(im, "n_frames", 1) > 1:
                return data, media_type

            width, height = im.size
            longest = max(width, height)
            oversized = longest > MAX_EDGE

            # Flatten transparency onto white: a JPEG cannot carry an alpha
            # channel, and a clinical photograph never depends on one.
            frame = im.convert("RGB") if im.mode not in {"RGB", "L"} else im.copy()
            if oversized:
                scale = MAX_EDGE / longest
                frame = frame.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    PILImage.LANCZOS,
                )

            buffer = io.BytesIO()
            frame.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            encoded = buffer.getvalue()
    except Exception:  # noqa: BLE001 - an unreadable image is the caller's problem
        logger.debug("Could not normalise an image for vision; sending it as-is")
        return data, media_type

    # An image over the cap is always sent shrunk, even on the rare occasion
    # that costs bytes. Providers price vision by pixel area, not by file size:
    # a sparse 2000px PNG can be smaller on the wire than its 1568px JPEG and
    # still be charged for every one of those pixels.
    if oversized:
        return encoded, "image/jpeg"

    # Within the cap there is nothing to gain from re-encoding unless it also
    # happens to shrink the upload, and a PNG screenshot usually grows.
    if len(encoded) >= len(data):
        return data, media_type
    return encoded, "image/jpeg"
