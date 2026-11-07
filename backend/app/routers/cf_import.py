"""Import a CurseForge modpack from a link or an uploaded zip, without an API key."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from ..models.cf_import import ImportPreview, LinkRequest, ServerPackCheck
from ..services import cf_import

router = APIRouter(prefix="/api/imports", tags=["imports"])


@router.post("/curseforge/preview", response_model=ImportPreview)
async def preview_link(req: LinkRequest):
    """Look up the pack behind a CurseForge link: its title and files."""
    return await cf_import.preview(req.url)


@router.get("/curseforge/{project_id}/files/{file_id}/server-pack", response_model=ServerPackCheck)
async def server_pack(project_id: int, file_id: int):
    """Whether a pack file has a server pack (None if CurseForge didn't say)."""
    return ServerPackCheck(has_server_pack=await cf_import.has_server_pack(str(project_id), str(file_id)))


@router.put("/upload", response_model=ImportPreview)
async def upload_pack(request: Request, filename: Annotated[str, Query(max_length=255)] = "pack.zip"):
    """Upload a pack zip downloaded from CurseForge. The body is the raw zip."""
    upload = cf_import.new_upload()
    try:
        with upload.open("wb") as out:
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > cf_import.MAX_PACK_BYTES:
                    raise cf_import.PackImportError("That file is too big.")
                out.write(chunk)
        return cf_import.describe_upload(upload.stem, filename)
    except cf_import.PackImportError as e:
        upload.unlink(missing_ok=True)
        raise HTTPException(400, str(e)) from e
