import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from .. import export
from ..export import ExportResult

router = APIRouter(prefix="/api/export", tags=["export"])

STATUSES = {"draft", "ready", "archived"}


def _validate(status: str | None) -> None:
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=400, detail="不支持的状态")


@router.get("/apkg")
@router.post("/apkg")
def download_apkg(
    module_id: int | None = Query(None, ge=1),
    status: str | None = None,
) -> FileResponse:
    _validate(status)
    try:
        result: ExportResult = export.build_apkg(module_id, status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return FileResponse(
        result.path,
        media_type="application/octet-stream",
        filename=result.filename,
        headers={"X-Cardmine-Missing-Media": str(len(result.missing_media))},
        background=BackgroundTask(result.cleanup),
    )


@router.get("/csv")
def download_csv(
    module_id: int | None = Query(None, ge=1),
    status: str | None = None,
) -> Response:
    _validate(status)
    content, cards = export.build_csv(module_id, status)
    if not cards:
        raise HTTPException(status_code=400, detail="没有符合条件的卡片可导出")

    filename = time.strftime("cardmine-%Y%m%d-%H%M%S.csv")
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Cardmine-Cards": str(cards),
        },
    )


@router.get("/backup/db")
def download_backup() -> FileResponse:
    path, filename = export.backup_database()
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        background=BackgroundTask(lambda: Path(path).unlink(missing_ok=True)),
    )


@router.get("/backup/media")
def download_media_manifest() -> Response:
    content = export.media_manifest_csv()
    filename = time.strftime("cardmine-media-%Y%m%d-%H%M%S.csv")
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
