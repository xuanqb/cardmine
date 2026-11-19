import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from .. import config
from ..db import utc_now
from ..importer import run_import

router = APIRouter(prefix="/api/import", tags=["import"])

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _set(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


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

    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)

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


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="导入任务不存在")
        return dict(job)
