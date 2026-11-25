"""Local session image upload and preview API."""

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from backend.storage.assets import AssetError, ImageAssetStore

from .session_store import require_active_session, session_store

router = APIRouter(prefix="/api/sessions/{session_id}/assets", tags=["assets"])


def asset_store(request: Request, session_id: str) -> ImageAssetStore:
    store = session_store(request.app.state.web)
    require_active_session(store, session_id)
    return ImageAssetStore(store, session_id)


@router.post("", status_code=201)
def upload_image(session_id: str, request: Request, file: UploadFile = File(...)):
    try:
        asset = asset_store(request, session_id).create(file.filename or "image", file.file)
        return {key: asset[key] for key in ("asset_id", "kind", "filename", "mime_type", "bytes", "width", "height")}
    except AssetError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{asset_id}")
def image_metadata(session_id: str, asset_id: str, request: Request):
    try:
        asset = asset_store(request, session_id).get(asset_id)
        return {key: asset[key] for key in ("asset_id", "kind", "filename", "mime_type", "bytes", "width", "height")}
    except AssetError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{asset_id}/content")
@router.head("/{asset_id}/content", include_in_schema=False)
def image_content(session_id: str, asset_id: str, request: Request):
    store = asset_store(request, session_id)
    try:
        asset = store.get(asset_id)
        return FileResponse(
            store.path(asset_id),
            media_type=asset["mime_type"],
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
    except AssetError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/{asset_id}")
def delete_image(session_id: str, asset_id: str, request: Request):
    try:
        asset_store(request, session_id).delete(asset_id)
        return {"deleted": asset_id}
    except AssetError as exc:
        raise HTTPException(409, str(exc)) from exc
