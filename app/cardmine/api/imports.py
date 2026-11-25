import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from .. import config
from ..collect.service import prepare_manual_post, run_manual_post
from ..db import utc_now
from ..importer import run_import
from ..importer.markdown import run_markdown_import
from .settings import load_settings

router = APIRouter(prefix="/api/import", tags=["import"])


class TextImportIn(BaseModel):
    title: str | None = None
    text: str
    group_id: int


_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _set(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _has_active_job() -> bool:
    with _lock:
        return any(job["status"] in ("queued", "running") for job in _jobs.values())


def _run_job(job_id: str, path: Path) -> None:
    def progress(phase: str, done: int, total: int) -> None:
        _set(job_id, phase=phase, processed=done, total=total)

    _set(job_id, status="running", started_at=utc_now())
    try:
        report = run_import(str(path), progress)
        _set(job_id, status="done", report=report.to_dict(), finished_at=utc_now())
    except Exception as exc:
        _set(job_id, status="failed", error=str(exc), finished_at=utc_now())
    finally:
        path.unlink(missing_ok=True)


@router.post("/apkg")
async def upload_apkg(file: UploadFile = File(...)) -> dict:
    filename = Path(file.filename or "import.apkg").name
    if not filename.lower().endswith((".apkg", ".colpkg")):
        raise HTTPException(status_code=400, detail="仅支持 .apkg / .colpkg 文件")

    with _lock:
        if any(job["status"] in ("queued", "running") for job in _jobs.values()):
            raise HTTPException(status_code=409, detail="已有导入任务在运行，请等待完成")

    upload_dir = config.DATA_DIR / "imports"
    upload_dir.mkdir(parents=True, exist_ok=True)

    job_id = uuid.uuid4().hex[:12]
    dest = upload_dir / f"{job_id}-{filename}"

    size = 0
    try:
        with open(dest, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > config.MAX_APKG_BYTES:
                    raise HTTPException(
                        status_code=413, detail="apkg 文件超过 1GB，已拒绝导入"
                    )
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise

    job = {
        "job_id": job_id,
        "filename": filename,
        "status": "queued",
        "phase": "排队中",
        "processed": 0,
        "total": 0,
        "report": None,
        "error": None,
        "created_at": utc_now(),
        "started_at": None,
        "finished_at": None,
    }
    with _lock:
        _jobs[job_id] = job

    threading.Thread(target=_run_job, args=(job_id, dest), daemon=True).start()
    return {"job_id": job_id}


@router.post("/markdown")
async def upload_markdown(
    file: UploadFile = File(...),
    module: str = Form("前端"),
    dry_run: bool = Form(False),
    overwrite: bool = Form(False),
) -> dict:
    filename = Path(file.filename or "questions.md").name
    if Path(filename).suffix.lower() not in (".md", ".markdown", ".zip"):
        raise HTTPException(status_code=400, detail="仅支持 .md / .markdown / .zip 文件")

    chunks = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > config.MAX_MARKDOWN_IMPORT_BYTES:
            raise HTTPException(
                status_code=413, detail="Markdown 题库文件超过 50MB，已拒绝导入"
            )
        chunks.append(chunk)

    try:
        report = run_markdown_import(
            filename,
            b"".join(chunks),
            module_path=module,
            dry_run=dry_run,
            overwrite=overwrite,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return report.to_dict()


@router.post("/text")
def start_text_import(payload: TextImportIn) -> dict:
    """粘贴整篇面经后直接入队，后台完成切题与归并。"""
    settings = load_settings()
    try:
        prepare_manual_post(settings, payload.title, payload.text, payload.group_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _has_active_job():
        raise HTTPException(status_code=409, detail="已有导入任务在运行，请等待完成")

    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "kind": "text",
            "status": "queued",
            "phase": "排队中",
            "processed": 0,
            "total": 0,
            "report": None,
            "error": None,
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
        }
    threading.Thread(
        target=_run_text_job,
        args=(job_id, payload.title, payload.text, payload.group_id),
        daemon=True,
    ).start()
    return {"job_id": job_id}


def _run_text_job(
    job_id: str, title: str | None, text: str, group_id: int
) -> None:
    def progress(phase: str, done: int, total: int) -> None:
        _set(job_id, phase=phase, processed=done, total=total)

    _set(job_id, status="running", started_at=utc_now())
    try:
        report = run_manual_post(load_settings(), title, text, group_id, progress)
        if report["ai_failed"]:
            error = report["errors"][0] if report["errors"] else "AI 切题失败"
            raise RuntimeError(error)
        _set(
            job_id,
            status="done",
            phase="完成",
            processed=1,
            total=1,
            report=report,
            finished_at=utc_now(),
        )
    except Exception as exc:  # noqa: BLE001 - 任务失败统一回传前端
        _set(job_id, status="failed", error=str(exc), finished_at=utc_now())


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="导入任务不存在")
        return dict(job)
