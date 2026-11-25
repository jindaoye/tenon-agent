"""Session-owned immutable images backed by json_objects and local files."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from backend.domain.images import ASSET_ID, MAX_IMAGES, ImageContent, ImageReference
from backend.domain.state import utc_now

if TYPE_CHECKING:
    from backend.storage.sqlite import SQLiteSessionStore

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
FORMATS = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"), "WEBP": ("image/webp", ".webp")}


class AssetError(ValueError):
    pass


class ImageAssetStore:
    def __init__(self, store: SQLiteSessionStore, session_id: str) -> None:
        self.store = store
        self.session_id = session_id
        self.root = store.paths.session_db(session_id).parent / "assets"

    def _path(self, asset_id: str, suffix: str) -> Path:
        if not ASSET_ID.fullmatch(asset_id) or suffix not in {p[1] for p in FORMATS.values()}:
            raise AssetError("Invalid image asset.")
        path = self.root / asset_id / f"original{suffix}"
        for part in (self.root, path.parent, path):
            if part.is_symlink() or (part.exists() and int(getattr(part.lstat(), "st_file_attributes", 0)) & 0x400):
                raise AssetError("Image asset links are not allowed.")
        return path

    def create(self, filename: str, stream: BinaryIO) -> dict[str, Any]:
        data = stream.read(MAX_IMAGE_BYTES + 1)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise AssetError("图片不能为空，单张图片最多 10 MiB。")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    width, height = image.size
                    if image.format not in FORMATS or getattr(image, "n_frames", 1) != 1:
                        raise AssetError("仅支持静态 PNG、JPEG、WebP 图片。")
                    if width * height > MAX_IMAGE_PIXELS:
                        raise AssetError("图片最多 2000 万像素。")
                    mime, suffix = FORMATS[image.format]
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise AssetError("图片内容无效或尺寸过大。") from exc
        asset_id = f"asset_{uuid4().hex}"
        path = self._path(asset_id, suffix)
        path.parent.mkdir(parents=True, exist_ok=False)
        asset = {
            "asset_id": asset_id,
            "kind": "image",
            "filename": Path(filename.replace("\\", "/")).name[:255],
            "mime_type": mime,
            "suffix": suffix,
            "storage_path": f"assets/{asset_id}/{path.name}",
            "bytes": len(data),
            "width": width,
            "height": height,
            "sha256": hashlib.sha256(data).hexdigest(),
            "created_at": utc_now(),
            "pinned": False,
        }
        try:
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, path)
            with self.store._connection(self.session_id, write=True, refresh_index=False) as connection:
                self.store._put_json_object(connection, self.session_id, "assets", asset_id, asset, utc_now())
        except BaseException:
            shutil.rmtree(path.parent, ignore_errors=True)
            raise
        return asset

    def get(self, asset_id: str) -> dict[str, Any]:
        if not isinstance(asset_id, str) or not ASSET_ID.fullmatch(asset_id):
            raise AssetError("Invalid image asset ID.")
        with self.store._connection(self.session_id, refresh_index=False) as connection:
            asset = self.store._json_object(connection, self.session_id, "assets", asset_id)
        if asset is None:
            raise AssetError("图片不存在或不属于当前会话。")
        return asset

    def path(self, asset_id: str) -> Path:
        asset = self.get(asset_id)
        path = self._path(asset_id, asset["suffix"])
        if not path.is_file():
            raise AssetError("图片文件缺失。")
        return path

    def pin(self, asset_ids: list[str]) -> list[ImageReference]:
        if len(asset_ids) > MAX_IMAGES:
            raise AssetError(f"每条消息最多 {MAX_IMAGES} 张图片。")
        for asset_id in asset_ids:
            self.path(asset_id)
        result = []
        with self.store._connection(self.session_id, write=True, refresh_index=False) as connection:
            for asset_id in dict.fromkeys(asset_ids):
                asset = self.store._json_object(connection, self.session_id, "assets", asset_id)
                if asset is None:
                    raise AssetError("图片不存在或不属于当前会话。")
                asset["pinned"] = True
                self.store._put_json_object(connection, self.session_id, "assets", asset_id, asset, utc_now())
                result.append(ImageReference(asset_id, asset["filename"], asset["width"], asset["height"]))
        return result

    def read(self, asset_id: str) -> ImageContent:
        asset = self.get(asset_id)
        with self.path(asset_id).open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
        if hashlib.sha256(data).hexdigest() != asset["sha256"]:
            raise AssetError("图片内容已改变，请重新上传。")
        return ImageContent(asset["mime_type"], data)

    def delete(self, asset_id: str) -> None:
        asset = self.get(asset_id)
        path = self._path(asset_id, asset["suffix"])
        with self.store._connection(self.session_id, write=True, refresh_index=False) as connection:
            asset = self.store._json_object(connection, self.session_id, "assets", asset_id)
            if asset is None:
                raise AssetError("图片不存在。")
            if asset["pinned"]:
                raise AssetError("图片已被消息引用，将随会话一起清理。")
            path.unlink(missing_ok=True)
            connection.execute(
                "DELETE FROM json_objects WHERE session_id=? AND namespace='assets' AND object_id=?",
                (self.session_id, asset_id),
            )
        path.parent.rmdir()
