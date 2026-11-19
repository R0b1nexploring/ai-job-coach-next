"""V2 application service, using the personal-install deletion journal guard."""
from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select

from app.content import stable_hash
from app.content_v2 import Claim, ClaimEvidence, FactSnapshot as FactContract, normalize_material_text
from app.facts_v2 import create_fact_snapshot, create_material_revision
from app.generation_v2 import build_writer_v2
from app.models import Batch, FactSnapshot, Guide, GuideRevision, Resume, ResumeRevision, new_id, utcnow
from app.privacy_v2 import bind_batch_snapshot, v2_write_transaction
from app.service import InputConflict, NotFound, validate_profile
from app.guide_v2 import content_hash, coverage_units, guide_input_hash, semantic_hash
from app.content_v2 import ResumeDocumentV2
from app.model_projection_v2 import project_facts_with_diagnostics


def create_v2_batch(session_factory, *, jd: str, material: str, candidates: list[dict],
                    header: dict | None, resume_locale: str, guide_locale: str,
                    idempotency_key: str, source_type: str = "manual", material_revision_id: str | None = None) -> dict:
    """Freeze user-selected candidates and queue a single-JD generation.

    Candidate positions refer to normalized input, not uploaded original bytes.
    Confirmation is exclusively the user's per-candidate decision. Nothing
    here invokes a model; callers must obtain consent for their provider.
    """
    if not 20 <= len(jd.strip()) <= 15000 or resume_locale not in {"zh-CN", "en"} or guide_locale not in {"zh-CN", "en"}:
        raise ValueError("岗位描述或语言无效")
    if not 8 <= len(idempotency_key) <= 128 or len(candidates) > 200:
        raise ValueError("请求格式无效")
    if source_type not in {"text", "docx", "pdf", "manual"}:
        raise ValueError("素材类型无效")
    text = normalize_material_text(material)
    if len(text) > 20000 or (not text and candidates):
        raise ValueError("素材或事实无效")
    profile = validate_profile(header)
    configuration = build_writer_v2().configuration
    frozen_input = {"schema_version": 2, "jd": jd.strip(), "material": text, "candidates": candidates,
                    "header": profile, "resume_locale": resume_locale, "guide_locale": guide_locale,
                    "source_type": source_type, "material_revision_id": material_revision_id,
                    "provider_configuration": configuration}
    request_hash = stable_hash(frozen_input)
    batch_id = new_id()
    with v2_write_transaction(session_factory, batch_id=batch_id) as session:
        existing = session.scalar(select(Batch).where(Batch.idempotency_key == idempotency_key))
        if existing:
            if existing.id in session.info["v2_journal_guard"]["deleted_ids"]:
                raise InputConflict()
            if existing.schema_version != 2 or existing.input_hash != request_hash:
                raise InputConflict()
            return {"id": existing.id, "status": existing.status}
        if material_revision_id:
            from app.materials_v2 import _validated_source, freeze_reused_material
            source = _validated_source(session, revision_id=material_revision_id)
            if source.extracted_text != text or source.source_type != source_type:
                raise ValueError("素材已变化，请重新读取并核对")
            reused = freeze_reused_material(session, revision_id=material_revision_id, candidates=candidates)
            fact_row = session.get(FactSnapshot, reused.fact_snapshot_id)
        else:
            source = create_material_revision(session, scope_id="local", source_type=source_type, extracted_text=text) if text else None
            claims = []
            confirmed = set()
            for index, item in enumerate(candidates):
                key = f"c{index + 1}"
                # The only accepted evidence is a hash-checked slice of the saved
                # source. User edits may change the value but never the source span.
                evidence = ClaimEvidence(source_revision_id=source.id, start=item["start"], end=item["end"], quote_hash=item["quote_hash"])
                state = "user_confirmed" if item.get("confirmed") is True else "unconfirmed"
                if state == "user_confirmed":
                    confirmed.add(key)
                claims.append(Claim(claim_id=key, fact_type=item["fact_type"], value=item["value"], evidence=(evidence,),
                                    confirmation=state, completion=item.get("completion", "unknown")))
            fact_row = create_fact_snapshot(session, scope_id="local", claims=claims, user_confirmed_claim_keys=frozenset(confirmed)).snapshot
        batch = Batch(id=batch_id, idempotency_key=idempotency_key, input_hash=request_hash,
                      jd_text=jd.strip(), material_text="", material_confirmed=False,
                      status="queued", attempts=0, schema_version=2, profile=deepcopy(profile),
                      target_snapshot={"jd_text": jd.strip(), "requirements": [], "inferred_requirements": []},
                      resume_locale=resume_locale, guide_locale=guide_locale,
                      generation_preferences={"provider_configuration": configuration, "schema_version": 2},
                      created_at=utcnow(), updated_at=utcnow())
        session.add(batch)
        session.flush()
        bind_batch_snapshot(session, batch_id=batch.id, snapshot_id=fact_row.id,
                            material_revision_ids=(source.id,) if source else ())
        batch.generation_input_hash = stable_hash({"scope": "local", "target": batch.target_snapshot,
                                                    "facts": fact_row.claims, "resume_locale": resume_locale,
                                                    "provider_configuration": configuration, "schema_version": 2})
        return {"id": batch.id, "status": batch.status}


def load_fact_snapshot(row: FactSnapshot) -> FactContract:
    return FactContract.model_validate({"snapshot_id": row.id, "private_scope": row.scope_id, "claims": row.claims})


def get_v2_batch(session_factory, batch_id: str) -> dict:
    """Read one installation-local batch while holding the deletion guard."""
    with v2_write_transaction(session_factory, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        if not batch or batch.schema_version != 2:
            raise NotFound()
        snapshot = session.get(FactSnapshot, batch.fact_snapshot_id)
        if snapshot is None or snapshot.scope_id != "local":
            raise NotFound()
        resumes = session.scalars(select(Resume).where(Resume.batch_id == batch.id)).all()
        guides = session.scalars(select(Guide).where(Guide.batch_id == batch.id)).all()
        diagnostics = project_facts_with_diagnostics(load_fact_snapshot(snapshot), private_header=batch.profile or {}, alias_identifiers=True).diagnostics
        return {"id": batch.id, "status": batch.status, "error_code": batch.error_code,
                "resume_locale": batch.resume_locale, "guide_locale": batch.guide_locale,
                "jd_text": batch.jd_text, "facts": deepcopy(snapshot.claims),
                "material_sources": [{"revision_id": item["revision_id"], "source_type": item["source_type"],
                                      "extracted_text": item["extracted_text"]} for item in snapshot.source_copies],
                "projection_exclusions": deepcopy(list(diagnostics.excluded)),
                "provider": (batch.generation_preferences or {}).get("provider_configuration", {}).get("provider"),
                "model": (batch.generation_preferences or {}).get("provider_configuration", {}).get("model"),
                "review": deepcopy((batch.plans or {}).get("v2_review", {"status": "not_run"})),
                "diagnostic_candidate": deepcopy((batch.plans or {}).get("candidate")) if batch.status == "review_blocked" else None,
                "diagnostic_documents": deepcopy((batch.plans or {}).get("diagnostic_documents")) if batch.status == "review_blocked" else None,
                "resumes": {r.kind: {"id": r.id, "revision": r.revision, "document": deepcopy(r.document_v2),
                                      "header": deepcopy(r.header), "plans": deepcopy(r.plans), "semantic_hash": r.semantic_hash,
                                      "content_hash": r.content_hash,
                                      "review": deepcopy((batch.plans or {}).get("revision_reviews", {}).get(r.kind, (batch.plans or {}).get("v2_review", {"status": "not_run"})))} for r in resumes},
                "guides": {g.kind: {"revision": g.revision, "resume_revision": g.resume_revision,
                                     "status": g.status, "error_code": g.error_code,
                                     "locale": g.guide_locale, "content": deepcopy(g.sections),
                                     "applies_to_revision": next((r.revision for r in resumes if r.id == g.resume_id and r.semantic_hash == g.semantic_hash and g.status == "ready"), None)} for g in guides}}


def list_v2_batches(session_factory) -> list[dict]:
    with v2_write_transaction(session_factory) as session:
        deleted = session.info["v2_journal_guard"]["deleted_ids"]
        query = select(Batch).where(Batch.schema_version == 2)
        if deleted:
            query = query.where(Batch.id.not_in(deleted))
        rows = session.scalars(query.order_by(Batch.created_at.desc()).limit(500)).all()
        return [{"id": row.id, "status": row.status, "created_at": row.created_at.isoformat(),
                 "job_label": row.jd_text.strip().splitlines()[0][:60] if row.jd_text.strip() else ""}
                for row in rows if row.id not in deleted]


def list_v2_history(session_factory, *, batch_id: str, kind: str) -> dict:
    if kind not in {"job", "blueprint"}:
        raise NotFound()
    with v2_write_transaction(session_factory, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        resume = session.scalar(select(Resume).where(Resume.batch_id == batch_id, Resume.kind == kind, Resume.schema_version == 2))
        guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == kind, Guide.schema_version == 2))
        if not batch or batch.schema_version != 2 or not resume:
            raise NotFound()
        revisions = session.scalars(select(ResumeRevision).where(ResumeRevision.resume_id == resume.id, ResumeRevision.schema_version == 2).order_by(ResumeRevision.revision.desc())).all()
        guides = session.scalars(select(GuideRevision).where(GuideRevision.guide_id == guide.id, GuideRevision.schema_version == 2).order_by(GuideRevision.revision.desc())).all() if guide else []
        return {"resumes": [{"revision": r.revision, "created_at": r.created_at.isoformat()} for r in revisions],
                "guides": [{"revision": g.revision, "resume_revision": g.resume_revision, "status": g.status,
                            "locale": g.guide_locale, "created_at": g.created_at.isoformat()} for g in guides]}


def retry_v2_batch(session_factory, *, batch_id: str) -> dict:
    with v2_write_transaction(session_factory, batch_id=batch_id) as session:
        batch = session.scalar(select(Batch).where(Batch.id == batch_id, Batch.schema_version == 2).with_for_update())
        if not batch:
            raise NotFound()
        if batch.status != "failed":
            raise InputConflict()
        batch.status, batch.error_code, batch.attempts = "queued", None, 0
        batch.claim_token, batch.lease_until, batch.updated_at = None, None, utcnow()
        return {"id": batch.id, "status": "queued"}


_PLAN_TEXT_FIELDS = frozenset({"title", "background", "task", "outcome", "verification"})
_PLAN_LIST_FIELDS = frozenset({"actions", "resource_constraints"})
_PLAN_REVIEW_FIELDS = frozenset({"background", "task", "outcome", "verification", "resource_constraints"})


def _plan_units(document: dict) -> dict[str, list[dict]]:
    """Ordered units per plan in this one document; lock is not semantic."""
    grouped: dict[str, list[dict]] = {}
    for section in document["sections"]:
        for entry in section["entries"]:
            for unit in entry["units"]:
                key = unit.get("plan_ref") or (entry.get("plan_ref") if section["type"] == "projects" else None)
                if key:
                    grouped.setdefault(key, []).append({
                        "entry_id": entry["entry_id"], **{name: deepcopy(unit.get(name)) for name in
                        ("unit_id", "text", "claim_refs", "authorship", "completion", "purpose")}})
            if section["type"] == "projects" and entry.get("plan_ref"):
                grouped.setdefault(entry["plan_ref"], [])
    return grouped


def _sync_v2_plans(old_plans: list[dict], before: dict, after: dict,
                   updates: dict[str, dict] | None) -> list[dict]:
    """Keep per-revision plan actions tied to the saved project units.

    Existing explanatory fields are retained and explicitly require review
    after a unit change. History retains prior actions; no AI or confirmation is
    implied. Call only inside the revision's guarded transaction.
    """
    if not isinstance(old_plans, list) or not all(isinstance(plan, dict) for plan in old_plans):
        raise ValueError("项目方案无效")
    plans = deepcopy(old_plans)
    keys = [plan.get("key") for plan in plans]
    if any(not isinstance(key, str) or not key.strip() for key in keys) or len(keys) != len(set(keys)):
        raise ValueError("项目方案标识无效")
    if updates is None:
        updates = {}
    if not isinstance(updates, dict) or len(updates) > 40 or not set(updates) <= set(keys):
        raise ValueError("项目方案不存在")
    old_groups, new_groups = _plan_units(before), _plan_units(after)
    old_summary, new_summary = before["summary"], after["summary"]
    old_summary_key, new_summary_key = old_summary.get("plan_ref"), new_summary.get("plan_ref")
    summary_refs = {key for key in (old_summary_key, new_summary_key) if key}
    if not (set(old_groups) | set(new_groups) | summary_refs) <= set(keys):
        raise ValueError("项目缺少绑定方案")
    if not set(updates) <= (set(new_groups) | ({new_summary_key} if new_summary_key else set())):
        raise ValueError("本版未引用该项目方案")
    for plan in plans:
        key = plan["key"]
        group_before, group_after = old_groups.get(key, []), new_groups.get(key, [])
        changed = group_before != group_after or (key in old_groups) != (key in new_groups)
        summary_changed = (key in summary_refs and
                           {k: v for k, v in old_summary.items() if k != "locked"} !=
                           {k: v for k, v in new_summary.items() if k != "locked"})
        changes = updates.get(key, {})
        if not isinstance(changes, dict) or not set(changes) <= _PLAN_TEXT_FIELDS | _PLAN_LIST_FIELDS:
            raise ValueError("项目方案字段无效")
        valid_review = plan.get("needs_review", [])
        if not isinstance(valid_review, list) or not all(isinstance(item, str) for item in valid_review):
            raise ValueError("方案核对状态无效")
        needs_review = set(valid_review)
        statuses = deepcopy(plan.get("field_authorship", {}))
        if not isinstance(statuses, dict):
            raise ValueError("方案来源状态无效")
        if summary_changed:
            needs_review.update(_PLAN_REVIEW_FIELDS)
        if changed:
            plan["actions"] = [unit["text"] for unit in group_after]
            plan["revision_units"] = deepcopy(group_after)
            needs_review.update(_PLAN_REVIEW_FIELDS)
            if not group_after:
                needs_review.add("actions")
            statuses["actions"] = "derived_from_saved_units"
        applied = False
        for field, value in changes.items():
            if field in _PLAN_TEXT_FIELDS:
                if not isinstance(value, str) or not value.strip() or len(value) > 2000:
                    raise ValueError("项目方案内容无效")
                value = value.strip()
            else:
                if not isinstance(value, list) or len(value) > 100 or any(not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in value):
                    raise ValueError("项目方案内容无效")
                value = [item.strip() for item in value]
                if field == "actions" and value != [unit["text"] for unit in group_after]:
                    raise ValueError("方案行动须与当前项目正文一致")
            if value != plan.get(field):
                applied = True
                plan[field] = deepcopy(value)
                statuses[field] = "user_edited_unconfirmed"
                needs_review.add(field)
        if changed or summary_changed or applied:
            plan["needs_review"] = sorted(needs_review)
            plan["field_authorship"] = statuses
    return plans


def save_v2_resume(session_factory, *, batch_id: str, kind: str, expected_revision: int,
                   title: str, unit_texts: dict[str, str], header: dict | None,
                   section_order: list[str] | None = None, delete_unit_ids: list[str] | None = None,
                   add_units: list[dict] | None = None, plan_updates: dict[str, dict] | None = None) -> dict:
    """Save a single-version text edit without changing fact or completion state."""
    if kind not in {"job", "blueprint"} or not title.strip() or len(title) > 160 or len(unit_texts) > 300:
        raise ValueError("编辑内容无效")
    if any(not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in unit_texts.values()):
        raise ValueError("正文不可为空或过长")
    delete_unit_ids, add_units = delete_unit_ids or [], add_units or []
    if len(delete_unit_ids) > 300 or len(set(delete_unit_ids)) != len(delete_unit_ids) or len(add_units) > 100:
        raise ValueError("正文单元操作无效")
    with v2_write_transaction(session_factory, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        if not batch or batch.schema_version != 2:
            raise NotFound()
        resume = session.scalar(select(Resume).where(Resume.batch_id == batch_id, Resume.kind == kind, Resume.schema_version == 2).with_for_update())
        if not resume:
            raise NotFound()
        if resume.revision != expected_revision:
            raise InputConflict()
        saved_header = validate_profile(header) if header is not None else deepcopy(resume.header or {})
        original_document = deepcopy(resume.document_v2)
        document = deepcopy(original_document)
        if section_order is not None:
            sections = {section["section_id"]: section for section in document["sections"]}
            if len(section_order) != len(sections) or set(section_order) != set(sections):
                raise ValueError("章节顺序无效")
            for position, section in enumerate(document["sections"]):
                if any(unit.get("locked") for entry in section["entries"] for unit in entry["units"]) and section_order[position] != section["section_id"]:
                    raise ValueError("请先解锁要移动的内容")
            document["sections"] = [sections[identifier] for identifier in section_order]
        units = [document["summary"]] + [unit for section in document["sections"] for entry in section["entries"] for unit in entry["units"]]
        by_id = {unit["unit_id"]: unit for unit in units}
        if not set(unit_texts) <= set(by_id) or not set(delete_unit_ids) <= (set(by_id) - {document["summary"]["unit_id"]}):
            raise ValueError("正文单元不存在")
        if set(unit_texts) & set(delete_unit_ids):
            raise ValueError("不能同时修改并删除同一单元")
        if any(by_id[identifier].get("locked") for identifier in delete_unit_ids):
            raise ValueError("请先解锁要删除的内容")
        for section in document["sections"]:
            for entry in section["entries"]:
                if any(unit.get("locked") for unit in entry["units"]) and any(unit["unit_id"] in delete_unit_ids for unit in entry["units"]):
                    raise ValueError("请先解锁该条目中的内容")
                entry["units"] = [unit for unit in entry["units"] if unit["unit_id"] not in delete_unit_ids]
        entries = {entry["entry_id"]: (section["type"], entry) for section in document["sections"] for entry in section["entries"]}
        for addition in add_units:
            if set(addition) != {"entry_id", "text"} or addition["entry_id"] not in entries:
                raise ValueError("新增单元的位置无效")
            value = addition["text"]
            if not isinstance(value, str) or not value.strip() or len(value) > 2000:
                raise ValueError("新增正文不可为空或过长")
            section_type, entry = entries[addition["entry_id"]]
            purpose = "skill" if section_type == "skills" else "proposal" if section_type == "projects" and kind == "blueprint" else "experience"
            entry["units"].append({"unit_id": new_id(), "text": value, "claim_refs": [], "support": [],
                                   "authorship": "user_edited", "completion": "planned" if purpose == "proposal" else "unknown",
                                   "purpose": purpose, "plan_ref": entry.get("plan_ref") if purpose == "proposal" else None, "locked": False})
        document["title"] = title.strip()
        for identifier, value in unit_texts.items():
            unit = by_id[identifier]
            if unit.get("locked") and value != unit["text"]:
                raise ValueError("请先解锁要修改的内容")
            if value != unit["text"]:
                unit["text"], unit["authorship"], unit["support"] = value, "user_edited", []
        document = ResumeDocumentV2.model_validate(document).model_dump(mode="json")
        facts = session.get(FactSnapshot, batch.fact_snapshot_id)
        if not facts or facts.scope_id != "local":
            raise NotFound()
        frozen_facts = {"snapshot_id": facts.id, "claims": deepcopy(facts.claims), "source_copies": deepcopy(facts.source_copies)}
        target = deepcopy(batch.target_snapshot)
        plans = _sync_v2_plans(resume.plans, original_document, document, plan_updates)
        chash = content_hash(document, saved_header, {"plans": plans, "fact_snapshot_id": facts.id, "target": target})
        shash = semantic_hash(document, target, frozen_facts, plans)
        if chash == resume.content_hash:
            return {"revision": resume.revision, "changed": False}
        old_hash = resume.semantic_hash
        next_revision = resume.revision + 1
        resume.revision, resume.document_v2, resume.header, resume.plans = next_revision, document, saved_header, deepcopy(plans)
        resume.title, resume.summary = document["title"], document["summary"]["text"]
        resume.content_hash, resume.semantic_hash = chash, shash
        session.add(ResumeRevision(resume_id=resume.id, revision=next_revision, title=resume.title, summary=resume.summary,
                                   header=deepcopy(saved_header), items=[], plans=plans, content_hash=chash,
                                   semantic_hash=shash, schema_version=2, document_v2=deepcopy(document)))
        if shash != old_hash:
            guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == kind).with_for_update())
            if guide:
                old_guide_revision = session.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id, GuideRevision.revision == guide.revision))
                if old_guide_revision and old_guide_revision.status in {"queued", "running"}:
                    old_guide_revision.status, old_guide_revision.error_code, old_guide_revision.completed_at = "superseded", "RESUME_CHANGED", utcnow()
                guide.status, guide.claim_token, guide.lease_until = "stale", None, None
            info = deepcopy(batch.plans or {})
            info.setdefault("revision_reviews", {})[kind] = {"status": "unavailable", "reason": "edited_after_review", "semantic_hash": shash}
            batch.plans = info
            batch.status = "guides_partial"
        batch.updated_at = utcnow()
        return {"revision": next_revision, "changed": True}


def retry_v2_guide(session_factory, *, batch_id: str, kind: str, guide_locale: str | None = None) -> dict:
    if kind not in {"job", "blueprint"} or (guide_locale is not None and guide_locale not in {"zh-CN", "en"}):
        raise ValueError("指南选择无效")
    with v2_write_transaction(session_factory, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        if not batch or batch.schema_version != 2:
            raise NotFound()
        resume = session.scalar(select(Resume).where(Resume.batch_id == batch_id, Resume.kind == kind, Resume.schema_version == 2).with_for_update())
        guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == kind, Guide.schema_version == 2).with_for_update())
        facts = session.get(FactSnapshot, batch.fact_snapshot_id)
        if not resume or not guide or not facts or facts.scope_id != "local":
            raise NotFound()
        locale = guide_locale or guide.guide_locale
        if guide.status not in {"stale", "failed"} and not (guide.status == "ready" and locale != guide.guide_locale):
            raise InputConflict()
        next_revision = guide.revision + 1
        coverage = coverage_units(resume.document_v2)
        input_hash = guide_input_hash(resume.semantic_hash, locale, coverage, "v2-s1")
        frozen_facts = {"snapshot_id": facts.id, "claims": deepcopy(facts.claims), "source_copies": deepcopy(facts.source_copies)}
        guide.revision, guide.resume_revision = next_revision, resume.revision
        guide.content_hash, guide.semantic_hash, guide.guide_input_hash = resume.content_hash, resume.semantic_hash, input_hash
        guide.guide_locale, guide.status, guide.sections = locale, "queued", None
        guide.attempts, guide.error_code, guide.claim_token, guide.lease_until = 0, None, None, None
        session.add(GuideRevision(guide_id=guide.id, batch_id=batch.id, revision=next_revision,
                                  resume_revision=resume.revision, content_hash=resume.content_hash,
                                  semantic_hash=resume.semantic_hash, resume_snapshot=deepcopy(resume.document_v2),
                                  plans=deepcopy(resume.plans), jd_text=batch.jd_text, status="queued", schema_version=2,
                                  guide_locale=locale, guide_input_hash=input_hash, coverage_unit_ids=coverage,
                                  v2_snapshot={"facts": frozen_facts, "target": deepcopy(batch.target_snapshot),
                                               "provider_configuration": deepcopy((batch.generation_preferences or {}).get("provider_configuration"))}))
        batch.status, batch.updated_at = "guides_pending", utcnow()
        return {"revision": next_revision, "status": "queued"}


def export_v2_document(session_factory, *, batch_id: str, kind: str, document_type: str,
                       file_format: str, revision: int | None = None, paper: str = "A4",
                       page_target: int = 1) -> tuple[bytes, dict]:
    """Render a saved revision under the journal lock; never call a model."""
    from app.files_v2 import bundled_converter_config, guide_blocks, render_docx, render_html, render_pdf, resume_blocks

    if kind not in {"job", "blueprint"} or document_type not in {"resume", "guide"} or file_format not in {"html", "docx", "pdf"}:
        raise ValueError("文件类型无效")
    if paper not in {"A4", "Letter"} or page_target not in {1, 2} or (revision is not None and revision < 1):
        raise ValueError("版式选择无效")
    with v2_write_transaction(session_factory, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        if not batch or batch.schema_version != 2 or batch.status == "review_blocked":
            raise NotFound()
        resume = session.scalar(select(Resume).where(Resume.batch_id == batch_id, Resume.kind == kind, Resume.schema_version == 2))
        if not resume:
            raise NotFound()
        if document_type == "resume":
            rev = session.scalar(select(ResumeRevision).where(ResumeRevision.resume_id == resume.id,
                ResumeRevision.revision == (revision or resume.revision), ResumeRevision.schema_version == 2))
            snapshot = session.get(FactSnapshot, batch.fact_snapshot_id)
            if not rev or not snapshot or snapshot.scope_id != "local":
                raise NotFound()
            document = ResumeDocumentV2.model_validate(rev.document_v2)
            locale = document.locale
            blocks = resume_blocks(document, load_fact_snapshot(snapshot), deepcopy(rev.header), draft=True)
            selected_revision = rev.revision
        else:
            guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == kind, Guide.schema_version == 2))
            if not guide:
                raise NotFound()
            if revision is None and guide.status != "ready":
                raise NotFound()
            grev = session.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id,
                GuideRevision.revision == (revision or guide.revision), GuideRevision.schema_version == 2))
            if not grev or grev.status != "ready" or not grev.sections:
                raise NotFound()
            locale = grev.guide_locale
            blocks = guide_blocks(deepcopy(grev.sections), locale, resume_revision=grev.resume_revision, kind=kind, draft=True)
            selected_revision = grev.revision
        meta = {"revision": selected_revision, "locale": locale, "draft": True, "paper": paper, "page_target": page_target}
        if file_format == "html":
            return render_html(blocks, locale, paper=paper, page_target=page_target), meta
        docx = render_docx(blocks, paper=paper, page_target=page_target)
        if file_format == "docx":
            return docx, meta
        pdf = render_pdf(docx, config=bundled_converter_config(), locale=locale, paper=paper, page_target=page_target)
        meta.update({"page_count": pdf.page_count, "exceeds_target": pdf.exceeds_target,
                     "layout_status": pdf.layout_status, "section_lengths": pdf.section_lengths})
        return pdf.data, meta


class PackageIncomplete(ValueError):
    def __init__(self, missing: list[str], overflows: list[dict] | None = None):
        self.missing = missing
        self.overflows = overflows or []
        super().__init__("完整草稿包尚不可用")


def export_v2_package(session_factory, *, batch_id: str, paper: str = "A4", page_target: int = 1,
                      guide_pagination: str = "match") -> bytes:
    """Create one consistent draft package from four saved, matching documents."""
    from hashlib import sha256
    from io import BytesIO
    import json
    from zipfile import ZIP_DEFLATED, ZipFile
    from app.files_v2 import bundled_converter_config, guide_blocks, render_docx, render_pdf, resume_blocks

    if paper not in {"A4", "Letter"} or page_target not in {1, 2} or guide_pagination not in {"match", "flow"}:
        raise ValueError("版式选择无效")
    with v2_write_transaction(session_factory, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        if not batch or batch.schema_version != 2 or batch.status == "review_blocked":
            raise NotFound()
        facts_row = session.get(FactSnapshot, batch.fact_snapshot_id)
        if not facts_row or facts_row.scope_id != "local":
            raise NotFound()
        facts = load_fact_snapshot(facts_row)
        records, missing = [], []
        for kind in ("job", "blueprint"):
            review = deepcopy((batch.plans or {}).get("revision_reviews", {}).get(kind, (batch.plans or {}).get("v2_review", {"status": "not_run"})))
            resume = session.scalar(select(Resume).where(Resume.batch_id == batch_id, Resume.kind == kind, Resume.schema_version == 2))
            guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == kind, Guide.schema_version == 2))
            if not resume:
                missing.append(f"{kind}:resume")
                continue
            saved = session.scalar(select(ResumeRevision).where(ResumeRevision.resume_id == resume.id,
                ResumeRevision.revision == resume.revision, ResumeRevision.schema_version == 2))
            if not saved:
                missing.append(f"{kind}:resume")
                continue
            document = ResumeDocumentV2.model_validate(saved.document_v2)
            records.append({"kind": kind, "type": "resume", "revision": saved.revision, "locale": document.locale,
                            "resume_revision": saved.revision, "content_hash": saved.content_hash, "resume_review": review.get("status", "not_run"),
                            "blocks": resume_blocks(document, facts, deepcopy(saved.header), draft=True)})
            if not guide or guide.status != "ready" or guide.semantic_hash != resume.semantic_hash:
                missing.append(f"{kind}:guide")
                continue
            grev = session.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id,
                GuideRevision.revision == guide.revision, GuideRevision.schema_version == 2))
            if not grev or grev.status != "ready" or not grev.sections or grev.semantic_hash != resume.semantic_hash:
                missing.append(f"{kind}:guide")
                continue
            records.append({"kind": kind, "type": "guide", "revision": grev.revision, "locale": grev.guide_locale,
                            "resume_revision": grev.resume_revision, "applies_to_revision": resume.revision,
                            "content_hash": grev.content_hash, "resume_review": review.get("status", "not_run"), "guide_review": "not_run",
                            "blocks": guide_blocks(deepcopy(grev.sections), grev.guide_locale,
                                                   resume_revision=grev.resume_revision, kind=kind, draft=True)})
        if missing:
            raise PackageIncomplete(missing)
        output, manifest, overflows = BytesIO(), [], []
        config = bundled_converter_config()
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for record in records:
                blocks = record.pop("blocks")
                docx = render_docx(blocks, paper=paper, page_target=page_target)
                pdf = render_pdf(docx, config=config, locale=record["locale"], paper=paper, page_target=page_target)
                if pdf.exceeds_target and (record["type"] == "resume" or guide_pagination == "match"):
                    overflows.append({"document": f'{record["kind"]}:{record["type"]}', "page_count": pdf.page_count,
                                      "page_target": page_target, "section_lengths": pdf.section_lengths})
                    continue
                for ext, data in (("docx", docx), ("pdf", pdf.data)):
                    name = f'draft-{record["kind"]}-{record["type"]}-r{record["revision"]}.{ext}'
                    archive.writestr(name, data)
                    manifest.append({**record, "file": name, "format": ext, "sha256": sha256(data).hexdigest(),
                                     "page_count": pdf.page_count, "purpose": "draft", "file_qa": "not_checked"})
            if overflows:
                raise PackageIncomplete([], overflows)
            archive.writestr("manifest.json", json.dumps({"schema_version": 2, "purpose": "draft", "paper": paper,
                "page_target": page_target, "guide_pagination": guide_pagination, "files": manifest}, ensure_ascii=False, indent=2))
        return output.getvalue()
