"""Durable image references; binary content belongs to the request boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_IMAGES = 4
ASSET_ID = re.compile(r"asset_[0-9a-f]{32}\Z")


@dataclass(frozen=True, slots=True)
class ImageReference:
    asset_id: str
    filename: str = "image"
    width: int = 0
    height: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.asset_id, str) or not ASSET_ID.fullmatch(self.asset_id):
            raise ValueError("Invalid image asset ID.")
        if not isinstance(self.filename, str) or any(
            isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (self.width, self.height)
        ):
            raise ValueError("Invalid image metadata.")

    def to_item(self) -> dict[str, Any]:
        return {
            "type": "image",
            "asset_id": self.asset_id,
            "filename": self.filename,
            "width": self.width,
            "height": self.height,
            "status": "success",
        }

    def to_reference(self) -> dict[str, str]:
        return {"source": "upload", "path": f"asset:{self.asset_id}", "display_path": self.filename}


@dataclass(frozen=True, slots=True)
class ImageContent:
    mime_type: str
    data: bytes


def image_budget(image: ImageReference) -> int:
    """Conservative patch budget, not a claim about provider billing rules."""
    import math

    return max(2048, math.ceil(max(1, image.width) / 32) * math.ceil(max(1, image.height) / 32) * 2)


def redact_image_data(value):
    """Copy wire data without retaining image bytes in events, logs or traces."""
    if isinstance(value, dict):
        return {key: redact_image_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_image_data(item) for item in value]
    if isinstance(value, str) and value.startswith("data:image/") and ";base64," in value:
        return value.partition(";base64,")[0] + ";base64,[OMITTED]"
    return value
