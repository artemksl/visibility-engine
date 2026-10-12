"""Narrow runtime private-image boundary, independent of T01 account snapshots.

Only opaque immutable references may be persisted. Resolved image bytes and local
paths never enter durable records or technical receipts. The synthetic resolver is
for controlled acceptance; production creative provisioning belongs to T06.
"""
from dataclasses import dataclass, field
from io import BytesIO
import re
import warnings

from PIL import Image

from automation_core.contracts import PrivateAssetRef

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


class PrivateAssetError(ValueError):
    def __init__(self, reason="private_asset_invalid"):
        self.reason = reason
        super().__init__(reason)


def _image_details(data):
    if type(data) is not bytes or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise PrivateAssetError()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format not in {"JPEG", "PNG"}:
                    raise PrivateAssetError()
                width, height = image.size
                if (not 0 < width * height <= MAX_IMAGE_PIXELS
                        or getattr(image, "n_frames", 1) != 1):
                    raise PrivateAssetError()
                media_type = "image/jpeg" if image.format == "JPEG" else "image/png"
                image.verify()
            # Verify checks structure; a complete decode also rejects truncation.
            with Image.open(BytesIO(data)) as image:
                image.load()
            return media_type, width, height
    except PrivateAssetError:
        raise
    except Exception:
        raise PrivateAssetError() from None


@dataclass(frozen=True)
class ResolvedPrivateAsset:
    reference: PrivateAssetRef
    data: bytes = field(repr=False)
    media_type: str
    width: int
    height: int

    def __post_init__(self):
        self.validate()

    def validate(self):
        if (type(self.reference) is not PrivateAssetRef or type(self.media_type) is not str or type(self.width) is not int
                or type(self.height) is not int
                or any(type(value) is not str or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value) is None
                       for value in (self.reference.provider_id, self.reference.reference_id, self.reference.version_id))
                or _image_details(self.data) != (self.media_type, self.width, self.height)):
            raise PrivateAssetError()

    def clear(self):
        """Release this runtime copy; callers resolve the exact version again."""
        object.__setattr__(self, "data", b"")

    def __reduce__(self):
        raise TypeError("Private assets cannot be serialized.")


def validated_private_asset(reference: PrivateAssetRef, data: bytes) -> ResolvedPrivateAsset:
    media_type, width, height = _image_details(data)
    return ResolvedPrivateAsset(reference, data, media_type, width, height)


