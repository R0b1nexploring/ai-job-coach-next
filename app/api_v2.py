"""V2 routes for the personal, loopback-only installation."""
import os
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.fact_candidates_v2 import extract_candidates
from app.privacy_v2 import DeletionPlanError, V2Deleted, delete_v2_batch
from app.service import InputConflict, NotFound, validate_profile
from app.service_v2 import (PackageIncomplete, create_v2_batch, export_v2_document, export_v2_package, get_v2_batch, list_v2_batches,
                            list_v2_history, retry_v2_batch, retry_v2_guide, save_v2_resume)
from app.files_v2 import ConversionUnavailable
from app.content_v2 import Claim, FactSnapshot
from app.model_projection_v2 import project_facts_with_diagnostics

router = APIRouter(prefix="/api/v2")


class CandidateInput(BaseModel):
    text: str = Field(max_length=20_000)


class FactInput(BaseModel):
    model_config = ConfigDict(extra="ignore")  # candidate UI may retain local display metadata
    fact_type: str = Field(min_length=1, max_length=80)
    value: str = Field(max_length=2000)
    start: int = Field(ge=0, le=20000, strict=True)
    end: int = Field(gt=0, le=20000, strict=True)
    quote_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: bool = Field(default=False, strict=True)
    completion: Literal["completed", "in_progress", "planned", "unknown", "not_applicable"] = "unknown"


class CreateV2Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jd_text: str = Field(min_length=20, max_length=15000)
    material_text: str = Field(default="", max_length=20000)
    candidates: list[FactInput] = Field(default_factory=list, max_length=200)
    header: dict | None = None
    resume_locale: Literal["zh-CN", "en"] = "zh-CN"
    guide_locale: Literal["zh-CN", "en"] | None = None
    source_type: Literal["text", "docx", "pdf", "manual"] = "manual"
    material_revision_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class ProjectionPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[FactInput] = Field(default_factory=list, max_length=200)
    header: dict | None = None


class EditV2Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    unit_texts: dict[str, str] = Field(default_factory=dict, max_length=300)
    header: dict | None = None
    section_order: list[str] | None = Field(default=None, max_length=30)
    delete_unit_ids: list[str] = Field(default_factory=list, max_length=300)
    add_units: list[dict] = Field(default_factory=list, max_length=100)
    plan_updates: dict[str, dict] = Field(default_factory=dict, max_length=40)


class RetryGuideV2Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guide_locale: Literal["zh-CN", "en"] | None = None


def _sessions():
    from app.api import SessionLocal
    return SessionLocal


def _not_found():
    return HTTPException(status_code=404, detail="未找到该记录或记录已删除")


def _require_v2_enabled():
    if os.getenv("COACH_V2_ENABLED") != "1":
        raise HTTPException(status_code=503, detail="新版流程尚未启用")


@router.post("/fact-candidates")
def fact_candidates(body: CandidateInput):
    try:
        return extract_candidates(body.text)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.post("/projection-preview")
def projection_preview(body: ProjectionPreviewBody):
    facts = tuple(Claim(claim_id=f"candidate-{index}", fact_type=item.fact_type, value=item.value,
                        confirmation="unconfirmed", completion=item.completion)
                  for index, item in enumerate(body.candidates))
    snapshot = FactSnapshot(snapshot_id="preview", private_scope="local", claims=facts)
    try:
        profile = validate_profile(body.header)
        diagnostics = project_facts_with_diagnostics(snapshot, private_header=profile, alias_identifiers=True).diagnostics
    except ValueError:
        raise HTTPException(status_code=422, detail="个人信息或事实输入无效") from None
    return {"excluded": [{"ordinal": item["ordinal"], "reason": item["reason"]} for item in diagnostics.excluded]}


@router.post("/batches", status_code=202, dependencies=[Depends(_require_v2_enabled)])
def create_batch(body: CreateV2Body, idempotency_key: str = Header(min_length=8, max_length=128)):
    try:
        return create_v2_batch(_sessions(), jd=body.jd_text, material=body.material_text,
                               candidates=[candidate.model_dump() for candidate in body.candidates],
                               header=body.header, resume_locale=body.resume_locale, guide_locale=body.guide_locale or body.resume_locale,
                               idempotency_key=idempotency_key, source_type=body.source_type, material_revision_id=body.material_revision_id)
    except InputConflict:
        raise HTTPException(status_code=409, detail="请求标识已用于不同内容") from None
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=422, detail="事实、来源或模型配置无效，请检查后重试") from None


@router.get("/batches")
def batches():
    return list_v2_batches(_sessions())


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: str):
    try:
        return get_v2_batch(_sessions(), batch_id)
    except (NotFound, V2Deleted, DeletionPlanError):
        raise _not_found() from None


@router.get("/batches/{batch_id}/history/{kind}")
def history(batch_id: str, kind: str):
    try:
        return list_v2_history(_sessions(), batch_id=batch_id, kind=kind)
    except (NotFound, V2Deleted, DeletionPlanError):
        raise _not_found() from None


@router.post("/batches/{batch_id}/retry", status_code=202, dependencies=[Depends(_require_v2_enabled)])
def retry_batch(batch_id: str):
    try:
        return retry_v2_batch(_sessions(), batch_id=batch_id)
    except (NotFound, V2Deleted, DeletionPlanError):
        raise _not_found() from None
    except InputConflict:
        raise HTTPException(status_code=409, detail="仅失败的任务可以重试") from None


@router.put("/batches/{batch_id}/resumes/{kind}", dependencies=[Depends(_require_v2_enabled)])
def edit_resume(batch_id: str, kind: str, body: EditV2Body):
    try:
        return save_v2_resume(_sessions(), batch_id=batch_id, kind=kind, **body.model_dump())
    except (NotFound, V2Deleted, DeletionPlanError):
        raise _not_found() from None
    except InputConflict:
        raise HTTPException(status_code=409, detail="简历已变化，请刷新后重新编辑") from None
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="编辑内容无效，请检查正文和个人信息") from None


@router.post("/batches/{batch_id}/guides/{kind}/retry", status_code=202, dependencies=[Depends(_require_v2_enabled)])
def retry_guide(batch_id: str, kind: str, body: RetryGuideV2Body | None = None):
    try:
        return retry_v2_guide(_sessions(), batch_id=batch_id, kind=kind, guide_locale=body.guide_locale if body else None)
    except (NotFound, V2Deleted, DeletionPlanError):
        raise _not_found() from None
    except InputConflict:
        raise HTTPException(status_code=409, detail="指南当前无法重新生成") from None
    except ValueError:
        raise HTTPException(status_code=422, detail="指南语言无效") from None


@router.get("/batches/{batch_id}/documents/{kind}/{document_type}/{file_format}", dependencies=[Depends(_require_v2_enabled)])
def export_document(batch_id: str, kind: str, document_type: str, file_format: str,
                    revision: int | None = Query(default=None, ge=1), paper: Literal["A4", "Letter"] = "A4",
                    page_target: int = Query(default=1, ge=1, le=2), allow_overflow: bool = False):
    try:
        content, meta = export_v2_document(_sessions(), batch_id=batch_id, kind=kind, document_type=document_type,
                                           file_format=file_format, revision=revision, paper=paper, page_target=page_target)
    except (NotFound, V2Deleted, DeletionPlanError):
        raise _not_found() from None
    except ConversionUnavailable:
        raise HTTPException(status_code=503, detail="PDF 转换器或字体不可用，HTML 和 Word 仍可下载") from None
    except ValueError:
        raise HTTPException(status_code=422, detail="文件或版式选择无效") from None
    if meta.get("exceeds_target") and not allow_overflow:
        return JSONResponse({"detail": "内容超过目标页数，请调整正文或选择两页。", "page_count": meta["page_count"],
                             "page_target": page_target, "section_lengths": meta["section_lengths"]}, status_code=409)
    mime = {"html": "text/html; charset=utf-8", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "pdf": "application/pdf"}[file_format]
    return Response(content, media_type=mime, headers={"Content-Disposition": f'attachment; filename="draft-{kind}-{document_type}-r{meta["revision"]}.{file_format}"',
                                                     "X-Document-Status": "draft", "Cache-Control": "no-store"})


@router.get("/batches/{batch_id}/package", dependencies=[Depends(_require_v2_enabled)])
def export_package(batch_id: str, paper: Literal["A4", "Letter"] = "A4", page_target: int = Query(default=1, ge=1, le=2),
                   guide_pagination: Literal["match", "flow"] = "match"):
    try:
        content = export_v2_package(_sessions(), batch_id=batch_id, paper=paper, page_target=page_target, guide_pagination=guide_pagination)
    except (NotFound, V2Deleted, DeletionPlanError):
        raise _not_found() from None
    except PackageIncomplete as error:
        return JSONResponse({"detail": "完整草稿包仍有缺项或超出目标页数。", "missing": error.missing, "overflows": error.overflows}, status_code=409)
    except ConversionUnavailable:
        raise HTTPException(status_code=503, detail="PDF 转换器或字体不可用，暂不能生成完整包") from None
    return Response(content, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="jobcoach-complete-draft.zip"',
                                                                    "X-Document-Status": "draft", "Cache-Control": "no-store"})


@router.delete("/batches/{batch_id}", status_code=204)
def delete_batch(batch_id: str):
    try:
        delete_v2_batch(_sessions(), batch_id=batch_id)
    except (NotFound, V2Deleted, DeletionPlanError):
        raise _not_found() from None
    return Response(status_code=204)
