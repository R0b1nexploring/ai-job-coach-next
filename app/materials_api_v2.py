"""Optional local-only independent material endpoints for the v2 workbench."""
from __future__ import annotations

import os
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.materials_v2 import (create_independent_material, list_independent_materials,
                              list_material_revisions, read_material_revision)
from app.privacy_v2 import (DeletionImpactChanged, DeletionPlanError, V2Deleted,
                            delete_independent_material, preview_independent_material_deletion)


def _enabled():
    if os.getenv("COACH_V2_ENABLED") != "1":
        raise HTTPException(status_code=503, detail="新版流程尚未启用")


def _sessions():
    from app.api import SessionLocal
    return SessionLocal


router = APIRouter(prefix="/api/v2/materials")


class MaterialBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extracted_text: str = Field(min_length=1, max_length=20_000)
    source_type: Literal["text", "manual", "docx", "pdf"] = "manual"


class DeleteMaterialBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["cascade", "preserve_existing"] = "cascade"
    impact_token: str = Field(pattern=r"^[0-9a-f]{64}$")


def _missing():
    return HTTPException(status_code=404, detail="未找到该素材或素材已删除")


@router.post("", status_code=201, dependencies=[Depends(_enabled)])
def create_material(body: MaterialBody):
    try:
        return create_independent_material(_sessions(), **body.model_dump())
    except V2Deleted:
        raise _missing() from None
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="素材内容无效") from None


@router.get("")
def list_materials():
    return {"materials": list_independent_materials(_sessions())}


@router.post("/{material_id}/revisions", status_code=201, dependencies=[Depends(_enabled)])
def append_material_revision(material_id: str, body: MaterialBody):
    try:
        return create_independent_material(_sessions(), material_id=material_id, **body.model_dump())
    except V2Deleted:
        raise _missing() from None
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="素材内容无效") from None


@router.get("/{material_id}/revisions", dependencies=[Depends(_enabled)])
def material_revisions(material_id: str):
    try:
        return {"revisions": list_material_revisions(_sessions(), material_id=material_id)}
    except V2Deleted:
        raise _missing() from None


@router.get("/{material_id}/revisions/{revision_id}", dependencies=[Depends(_enabled)])
def material_revision(material_id: str, revision_id: str):
    try:
        return read_material_revision(_sessions(), material_id=material_id, revision_id=revision_id)
    except V2Deleted:
        raise _missing() from None
    except ValueError:
        raise HTTPException(status_code=503, detail="素材校验失败，请检查资料") from None


@router.get("/{material_id}/deletion-preview")
def preview_material_deletion(material_id: str, mode: Literal["cascade", "preserve_existing"] = Query(default="cascade")):
    try:
        return preview_independent_material_deletion(_sessions(), material_id=material_id, mode=mode)
    except V2Deleted:
        raise _missing() from None
    except DeletionPlanError:
        raise HTTPException(status_code=409, detail="素材关联暂不可安全删除，请检查关联资料") from None


@router.delete("/{material_id}", status_code=204)
def delete_material(material_id: str, body: DeleteMaterialBody):
    try:
        delete_independent_material(_sessions(), material_id=material_id, mode=body.mode,
                                    expected_impact_token=body.impact_token)
    except DeletionImpactChanged:
        raise HTTPException(status_code=409, detail="关联批次已变化，请重新查看删除影响") from None
    except V2Deleted:
        raise _missing() from None
    except DeletionPlanError:
        raise HTTPException(status_code=409, detail="素材关联暂不可安全删除，请重新查看删除影响") from None
    return Response(status_code=204)
