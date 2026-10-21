from __future__ import annotations

from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from app.db import SessionLocal
from app.files import extract_material, render_docx, render_guide_html, render_html, render_pdf
from app.service import (InputConflict, NotFound, create_batch, get_batch, list_batches,
    list_revisions, retry_guide, save_resume, retry_batch, get_resume_revision,
    list_guide_revisions, get_guide_revision, list_executions)
from app.privacy import JournalUnavailable, delete_batch, deleted_ids, initialize_journal, replay_deletions


@asynccontextmanager
async def lifespan(_app):
    from app.runtime import validate_configuration
    validate_configuration()
    initialize_journal(SessionLocal, bootstrap=True)
    replay_deletions(SessionLocal)
    yield

app = FastAPI(title="双版本简历工作台", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
STATIC = Path(__file__).parent / "static"


@app.exception_handler(JournalUnavailable)
async def journal_error(_request, _error):
    return JSONResponse({"detail": "删除记录不可用，请检查数据目录"}, status_code=503)


@app.middleware("http")
async def local_browser_boundary(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin:
            supplied = urlsplit(origin)
            expected = urlsplit(str(request.url))
            if supplied.scheme != expected.scheme or supplied.netloc != expected.netloc or supplied.path or supplied.query or supplied.fragment:
                return JSONResponse({"detail": "请从本机工作台访问"}, status_code=403)
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            return JSONResponse({"detail": "请从本机工作台访问"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            try:
                if int(request.headers.get("content-length", "0")) > 9 * 1024 * 1024:
                    return JSONResponse({"detail": "请求内容过大，文件上限为 8 MB"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "请求格式无效"}, status_code=400)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    response.headers["Cache-Control"] = "no-store"
    return response


def ensure_available(batch_id: str):
    try:
        if batch_id in deleted_ids():
            raise HTTPException(status_code=404, detail="该记录已删除")
    except JournalUnavailable:
        raise HTTPException(status_code=503, detail="删除记录不可用，请检查数据目录") from None


class CreateBatchBody(BaseModel):
    jd_text: str = Field(min_length=20, max_length=15000)
    material_text: str = Field(default="", max_length=20000)
    material_confirmed: bool = False


class SaveResumeBody(BaseModel):
    revision: int = Field(ge=1)
    title: str = Field(min_length=2, max_length=160)
    summary: str = Field(min_length=4, max_length=1000)
    header: dict | None = None
    items: list[dict] = Field(min_length=1, max_length=20)
    plans: list[dict] | None = Field(default=None, max_length=20)


@app.get("/")
def index():
    content = (STATIC / "index.html").read_text()
    for name in ("style.css", "app.js"):
        version = sha256((STATIC / name).read_bytes()).hexdigest()[:12]
        content = content.replace(f'/static/{name}"', f'/static/{name}?v={version}"')
    return Response(content, media_type="text/html")


@app.get("/static/{name}")
def static(name: str):
    if name not in {"app.js", "style.css"}:
        raise HTTPException(status_code=404)
    return FileResponse(STATIC / name)


@app.get("/api/health")
def health():
    from app.runtime import database_health, worker_health
    database, worker = database_health(), worker_health()
    try:
        deleted_ids()
        journal = {"ok": True}
    except JournalUnavailable:
        journal = {"ok": False}
    ok = database["ok"] and worker["ok"] and journal["ok"]
    return JSONResponse({"status": "ok" if ok else "unhealthy", "scope": "personal_local", "database": database, "worker": worker, "journal": journal}, status_code=200 if ok else 503)


@app.get("/api/config")
def public_config():
    import os
    return {"provider": os.getenv("COACH_PROVIDER", "local"), "scope": "personal_local"}


@app.post("/api/batches", status_code=202)
def create(body: CreateBatchBody, idempotency_key: str = Header(min_length=8, max_length=128)):
    try:
        batch = create_batch(SessionLocal, jd=body.jd_text.strip(), material=body.material_text.strip(), confirmed=body.material_confirmed, idempotency_key=idempotency_key)
    except InputConflict:
        raise HTTPException(status_code=409, detail="请求标识对应的输入已变化") from None
    return {"id": batch.id, "status": batch.status, "status_url": f"/api/batches/{batch.id}"}


@app.get("/api/batches/latest")
def latest():
    hidden = deleted_ids()
    row = next((item for item in list_batches(SessionLocal, limit=1000) if item["id"] not in hidden), None)
    result = get_batch(SessionLocal, row["id"]) if row else None
    if not result:
        raise HTTPException(status_code=404, detail="暂无生成记录")
    return result


@app.get("/api/batches")
def history():
    hidden = deleted_ids()
    return [row for row in list_batches(SessionLocal) if row["id"] not in hidden]


@app.get("/api/batches/{batch_id}")
def detail(batch_id: str):
    ensure_available(batch_id)
    try:
        return get_batch(SessionLocal, batch_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="未找到该生成记录") from None


@app.post("/api/batches/{batch_id}/retry", status_code=202)
def retry_batch_route(batch_id: str):
    ensure_available(batch_id)
    try:
        return retry_batch(SessionLocal, batch_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="未找到该生成记录") from None
    except InputConflict:
        raise HTTPException(status_code=409, detail="仅失败的生成任务可以重试") from None


@app.delete("/api/batches/{batch_id}", status_code=204)
def delete_batch_route(batch_id: str):
    try:
        delete_batch(SessionLocal, batch_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="未找到该生成记录") from None
    return Response(status_code=204)


@app.get("/api/batches/{batch_id}/archive")
def batch_archive(batch_id: str):
    import json
    ensure_available(batch_id)
    try:
        batch = get_batch(SessionLocal, batch_id)
        archive = {"format_version": 1, "batch": batch,
                   "resume_history": {kind: list_revisions(SessionLocal, batch_id, kind) for kind in batch["resumes"]},
                   "guide_history": {kind: list_guide_revisions(SessionLocal, batch_id, kind) for kind in batch["guides"]},
                   "executions": list_executions(SessionLocal, batch_id)}
    except NotFound:
        raise HTTPException(status_code=404, detail="未找到该生成记录") from None
    return Response(json.dumps(archive, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="coach-data.json"'})


@app.get("/api/batches/{batch_id}/executions")
def executions(batch_id: str):
    ensure_available(batch_id)
    try:
        return list_executions(SessionLocal, batch_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="未找到该生成记录") from None


@app.post("/api/batches/{batch_id}/guides/{kind}/retry", status_code=202)
def retry_guide_route(batch_id: str, kind: str):
    ensure_available(batch_id)
    if kind not in {"job", "blueprint"}:
        raise HTTPException(status_code=404)
    try:
        retry_guide(SessionLocal, batch_id, kind)
    except NotFound:
        raise HTTPException(status_code=404, detail="未找到该指南") from None
    except InputConflict:
        raise HTTPException(status_code=409, detail="该指南当前无法重试") from None
    return {"status": "queued"}


@app.post("/api/materials/extract")
async def extract(file: UploadFile = File()):
    data = await file.read(8 * 1024 * 1024 + 1)
    try:
        return {"text": extract_material(file.filename or "", data)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.put("/api/batches/{batch_id}/resumes/{kind}")
def save_resume_route(batch_id: str, kind: str, body: SaveResumeBody):
    ensure_available(batch_id)
    if kind not in {"job", "blueprint"}:
        raise HTTPException(status_code=404)
    try:
        return save_resume(SessionLocal, batch_id, kind, **body.model_dump())
    except NotFound:
        raise HTTPException(status_code=404, detail="未找到该简历") from None
    except InputConflict:
        raise HTTPException(status_code=409, detail="内容已变化或锁定项被修改，请刷新后核对") from None
    except ValueError:
        raise HTTPException(status_code=422, detail="简历内容不完整或格式无效") from None


@app.get("/api/batches/{batch_id}/resumes/{kind}/revisions")
def revisions(batch_id: str, kind: str):
    ensure_available(batch_id)
    if kind not in {"job", "blueprint"}:
        raise HTTPException(status_code=404)
    try:
        return list_revisions(SessionLocal, batch_id, kind)
    except NotFound:
        raise HTTPException(status_code=404, detail="未找到该简历") from None


@app.get("/api/batches/{batch_id}/guides/{kind}/revisions")
def guide_revisions(batch_id: str, kind: str):
    ensure_available(batch_id)
    if kind not in {"job", "blueprint"}:
        raise HTTPException(status_code=404)
    try:
        return list_guide_revisions(SessionLocal, batch_id, kind)
    except NotFound:
        raise HTTPException(status_code=404, detail="未找到该指南") from None


@app.get("/api/batches/{batch_id}/guides/{kind}/revisions/{revision}/download")
def historical_guide(batch_id: str, kind: str, revision: int):
    ensure_available(batch_id)
    if kind not in {"job", "blueprint"}:
        raise HTTPException(status_code=404)
    try:
        guide = get_guide_revision(SessionLocal, batch_id, kind, revision)
    except NotFound:
        raise HTTPException(status_code=404, detail="未找到该指南修订") from None
    if guide["status"] != "ready":
        raise HTTPException(status_code=409, detail="该历史指南未成功生成")
    resume = {**guide["resume"], "revision": guide["resume_revision"]}
    return Response(render_guide_html(guide, resume), media_type="text/html; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="guide-{kind}-g{revision}-resume-v{guide["resume_revision"]}.html"'})


@app.get("/api/batches/{batch_id}/resumes/{kind}/download/{format}")
def download(batch_id: str, kind: str, format: str, revision: int | None = None):
    ensure_available(batch_id)
    if kind not in {"job", "blueprint"} or format not in {"html", "docx", "pdf"}:
        raise HTTPException(status_code=404)
    try:
        resume = get_resume_revision(SessionLocal, batch_id, kind, revision) if revision is not None else get_batch(SessionLocal, batch_id)["resumes"][kind]
    except (NotFound, KeyError):
        raise HTTPException(status_code=404, detail="未找到该简历") from None
    renderer = {"html": render_html, "docx": render_docx, "pdf": render_pdf}[format]
    try:
        content = renderer(resume)
    except Exception:
        raise HTTPException(status_code=503, detail="文件生成失败，请稍后重试") from None
    media = {"html": "text/html; charset=utf-8", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "pdf": "application/pdf"}[format]
    return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="resume-{kind}-v{resume["revision"]}.{format}"', "Cache-Control": "no-store"})


@app.get("/api/batches/{batch_id}/guides/{kind}/download")
def download_guide(batch_id: str, kind: str):
    ensure_available(batch_id)
    if kind not in {"job", "blueprint"}:
        raise HTTPException(status_code=404)
    try:
        batch = get_batch(SessionLocal, batch_id)
        guide, resume = batch["guides"][kind], batch["resumes"][kind]
    except (NotFound, KeyError):
        raise HTTPException(status_code=404, detail="未找到该指南") from None
    if guide["status"] != "ready":
        raise HTTPException(status_code=409, detail="指南尚未更新")
    bound = get_guide_revision(SessionLocal, batch_id, kind, guide["revision"])
    resume = {**bound["resume"], "revision": bound["resume_revision"]}
    return Response(render_guide_html(bound, resume), media_type="text/html; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="guide-{kind}-g{bound["revision"]}-resume-v{resume["revision"]}.html"', "Cache-Control": "no-store"})
