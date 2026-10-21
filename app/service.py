from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.content import normalize_bundle, normalize_guide, stable_hash
from app.models import Batch, Guide, GuideRevision, Resume, ResumeRevision, TaskExecution, new_id, utcnow
from app.providers import FallbackProvider, LocalProvider, Provider

MAX_ATTEMPTS = 3
LEASE_SECONDS = 900


class InputConflict(Exception):
    pass


class NotFound(Exception):
    pass


@dataclass(frozen=True)
class Claim:
    resource_id: str
    batch_id: str
    token: str
    input_hash: str
    snapshot: dict


def semantic_hash(data: dict, plans: list[dict], jd: str) -> str:
    """Locks affect editing, not what the user must explain in an interview."""
    items = [{key: value for key, value in item.items() if key != "locked"} for item in data["items"]]
    return stable_hash({"jd": jd, "title": data["title"], "summary": data["summary"], "items": items, "plans": plans})


def _resume_data(resume: Resume) -> dict:
    return {"title": resume.title, "summary": resume.summary, "header": deepcopy(resume.header), "items": deepcopy(resume.items)}


_HEADER_LIMITS = {"name": 80, "phone": 40, "email": 160, "location": 80, "education": 1500}


def _clean_header(header: dict) -> dict[str, str]:
    """Only explicit user-entered display fields belong in the resume header."""
    if not isinstance(header, dict) or set(header) - _HEADER_LIMITS.keys():
        raise ValueError("invalid resume header fields")
    cleaned = {}
    for field, value in header.items():
        if not isinstance(value, str):
            raise ValueError("invalid resume header value")
        if field != "education" and ("\r" in value or "\n" in value):
            raise ValueError("resume header requires single-line values")
        value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(value) > _HEADER_LIMITS[field]:
            raise ValueError("resume header value too long")
        if value:
            cleaned[field] = value
    return cleaned


def _content_hash(data: dict, plans: list[dict]) -> str:
    payload = {**data, "plans": plans}
    # Empty optional fields do not change hashes of pre-header revisions.
    if not payload.get("header"):
        payload.pop("header", None)
    return stable_hash(payload)


def _guide_input(resume: dict) -> dict:
    # Contact and education fields are a private, user-filled layout section;
    # project interview generation must never forward them to a provider.
    return {key: deepcopy(resume[key]) for key in ("title", "summary", "items")}


def _bound_plans(plans: list[dict], items: list[dict]) -> list[dict]:
    keys = {item["plan_key"] for item in items}
    return deepcopy([plan for plan in plans if plan["key"] in keys])


def create_batch(session_factory: Callable[[], Session], *, jd: str, material: str, confirmed: bool, idempotency_key: str) -> Batch:
    input_hash = stable_hash({"jd": jd, "material": material, "confirmed": confirmed})
    with session_factory() as session:
        existing = session.scalar(select(Batch).where(Batch.idempotency_key == idempotency_key))
        if existing:
            if existing.input_hash != input_hash:
                raise InputConflict()
            return existing
        batch = Batch(id=new_id(), idempotency_key=idempotency_key, input_hash=input_hash, jd_text=jd, material_text=material, material_confirmed=confirmed, status="queued", attempts=0, created_at=utcnow(), updated_at=utcnow())
        session.add(batch)
        try:
            session.commit()
        except IntegrityError:
            # A concurrent request can win between the SELECT and INSERT.
            session.rollback()
            existing = session.scalar(select(Batch).where(Batch.idempotency_key == idempotency_key))
            if existing is None:
                raise
            if existing.input_hash != input_hash:
                raise InputConflict() from None
            return existing
        return batch


def _lock_batch(session: Session, batch_id: str) -> Batch | None:
    return session.scalar(select(Batch).where(Batch.id == batch_id).with_for_update())


def _finish_execution(session: Session, token: str | None, status: str, error_code: str | None = None) -> None:
    if token:
        session.execute(update(TaskExecution).where(TaskExecution.claim_token == token, TaskExecution.status == "running").values(status=status, error_code=error_code, finished_at=utcnow()).execution_options(synchronize_session=False))


def _add_execution(session: Session, claim: Claim, task_type: str, attempt: int, guide_id: str | None = None) -> None:
    session.add(TaskExecution(id=new_id(), batch_id=claim.batch_id, guide_id=guide_id, task_type=task_type, claim_token=claim.token, attempt=attempt, input_hash=claim.input_hash, input_snapshot=deepcopy(claim.snapshot), status="running", started_at=utcnow()))


def _refresh_batch_status(session: Session, batch_id: str) -> None:
    batch = _lock_batch(session, batch_id)
    if not batch or batch.status in {"failed", "queued", "running"}:
        return
    statuses = list(session.scalars(select(Guide.status).where(Guide.batch_id == batch_id)))
    if len(statuses) == 2 and all(status == "ready" for status in statuses):
        batch.status = "ready"
    elif any(status in {"failed", "stale"} for status in statuses):
        batch.status = "guides_partial"
    else:
        batch.status = "guides_pending"
    batch.updated_at = utcnow()


def _guide_revision(session: Session, guide: Guide) -> GuideRevision:
    row = session.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id, GuideRevision.revision == guide.revision))
    if row is None:
        raise ValueError("missing guide snapshot")
    return row


def _claim_batch(session_factory: Callable[[], Session]) -> Claim | None:
    now = utcnow()
    expired = or_(Batch.lease_until.is_(None), Batch.lease_until <= now)
    with session_factory() as session:
        exhausted = session.scalars(select(Batch).where(Batch.status == "running", expired, Batch.attempts >= MAX_ATTEMPTS)).all()
        for batch in exhausted:
            result = session.execute(update(Batch).where(Batch.id == batch.id, Batch.status == "running", expired, Batch.attempts >= MAX_ATTEMPTS, Batch.claim_token == batch.claim_token).values(status="failed", error_code="ATTEMPTS_EXHAUSTED", lease_until=None, claim_token=None, updated_at=now).execution_options(synchronize_session=False))
            if result.rowcount:
                _finish_execution(session, batch.claim_token, "lease_expired", "ATTEMPTS_EXHAUSTED")
        candidates = session.scalars(select(Batch).where(or_(Batch.status == "queued", and_(Batch.status == "running", expired)), Batch.attempts < MAX_ATTEMPTS).order_by(Batch.created_at, Batch.id).limit(5)).all()
        for batch in candidates:
            old_token = batch.claim_token
            token = new_id()
            attempt = batch.attempts + 1
            result = session.execute(update(Batch).where(Batch.id == batch.id, Batch.attempts == batch.attempts, Batch.claim_token == old_token, or_(Batch.status == "queued", and_(Batch.status == "running", expired))).values(status="running", attempts=attempt, claim_token=token, lease_until=now + timedelta(seconds=LEASE_SECONDS), updated_at=now).execution_options(synchronize_session=False))
            if result.rowcount:
                _finish_execution(session, old_token, "lease_expired", "LEASE_EXPIRED")
                snapshot = {"jd": batch.jd_text, "material": batch.material_text, "confirmed": batch.material_confirmed}
                claim = Claim(batch.id, batch.id, token, batch.input_hash, snapshot)
                _add_execution(session, claim, "batch", attempt)
                session.commit()
                return claim
        session.commit()
    return None


def _claim_guide(session_factory: Callable[[], Session]) -> Claim | None:
    now = utcnow()
    expired = or_(Guide.lease_until.is_(None), Guide.lease_until <= now)
    with session_factory() as session:
        exhausted = session.scalars(select(Guide).where(Guide.status == "running", expired, Guide.attempts >= MAX_ATTEMPTS)).all()
        for guide in exhausted:
            if _lock_batch(session, guide.batch_id) is None:
                continue
            result = session.execute(update(Guide).where(Guide.id == guide.id, Guide.status == "running", expired, Guide.attempts >= MAX_ATTEMPTS, Guide.claim_token == guide.claim_token).values(status="failed", error_code="ATTEMPTS_EXHAUSTED", lease_until=None, claim_token=None).execution_options(synchronize_session=False))
            if result.rowcount:
                _finish_execution(session, guide.claim_token, "lease_expired", "ATTEMPTS_EXHAUSTED")
                revision = _guide_revision(session, guide)
                revision.status, revision.error_code, revision.completed_at = "failed", "ATTEMPTS_EXHAUSTED", now
                session.flush()
                _refresh_batch_status(session, guide.batch_id)
        candidates = session.scalars(select(Guide).join(Batch, Batch.id == Guide.batch_id).where(or_(Guide.status == "queued", and_(Guide.status == "running", expired)), Guide.attempts < MAX_ATTEMPTS).order_by(Batch.created_at, Guide.kind.desc(), Guide.id).limit(5)).all()
        for guide in candidates:
            if _lock_batch(session, guide.batch_id) is None:
                continue
            old_token = guide.claim_token
            token = new_id()
            attempt = guide.attempts + 1
            result = session.execute(update(Guide).where(Guide.id == guide.id, Guide.revision == guide.revision, Guide.attempts == guide.attempts, Guide.claim_token == old_token, or_(Guide.status == "queued", and_(Guide.status == "running", expired))).values(status="running", attempts=attempt, claim_token=token, lease_until=now + timedelta(seconds=LEASE_SECONDS)).execution_options(synchronize_session=False))
            if result.rowcount:
                revision = _guide_revision(session, guide)
                snapshot = {"kind": guide.kind, "guide_revision": guide.revision, "resume_revision": revision.resume_revision, "resume": deepcopy(revision.resume_snapshot), "plans": deepcopy(revision.plans), "jd": revision.jd_text, "content_hash": revision.content_hash, "semantic_hash": revision.semantic_hash}
                _finish_execution(session, old_token, "lease_expired", "LEASE_EXPIRED")
                revision.status = "running"
                claim = Claim(guide.id, guide.batch_id, token, stable_hash(snapshot), snapshot)
                _add_execution(session, claim, "guide", attempt, guide.id)
                session.commit()
                return claim
        session.commit()
    return None


def renew_execution(session_factory: Callable[[], Session], claim_token: str, lease_seconds: int = LEASE_SECONDS) -> bool:
    if not 1 <= lease_seconds <= LEASE_SECONDS:
        raise ValueError("invalid lease duration")
    now = utcnow()
    with session_factory() as session:
        execution = session.scalar(select(TaskExecution).where(TaskExecution.claim_token == claim_token, TaskExecution.status == "running"))
        if execution is None:
            return False
        model = Guide if execution.guide_id else Batch
        resource_id = execution.guide_id or execution.batch_id
        result = session.execute(update(model).where(model.id == resource_id, model.status == "running", model.claim_token == claim_token, model.lease_until > now).values(lease_until=now + timedelta(seconds=lease_seconds)).execution_options(synchronize_session=False))
        session.commit()
        return bool(result.rowcount)


def _batch_owned(claim: Claim):
    return and_(Batch.id == claim.resource_id, Batch.status == "running", Batch.claim_token == claim.token, Batch.input_hash == claim.input_hash, Batch.lease_until > utcnow())


def _guide_owned(claim: Claim):
    snap = claim.snapshot
    current_resume_matches = exists(select(Resume.id).where(Resume.id == Guide.resume_id, Resume.semantic_hash == snap["semantic_hash"]))
    return and_(Guide.id == claim.resource_id, Guide.status == "running", Guide.claim_token == claim.token, Guide.revision == snap["guide_revision"], Guide.content_hash == snap["content_hash"], Guide.semantic_hash == snap["semantic_hash"], Guide.lease_until > utcnow(), current_resume_matches)


def _reject_execution(session: Session, claim: Claim, model) -> None:
    expired = session.scalar(select(model.id).where(model.id == claim.resource_id, model.claim_token == claim.token, or_(model.lease_until.is_(None), model.lease_until <= utcnow())))
    status = "lease_expired" if expired else "superseded"
    _finish_execution(session, claim.token, status, "LEASE_EXPIRED" if status == "lease_expired" else "OWNERSHIP_LOST")


def _source(provider: Provider) -> str:
    return provider.last_source if isinstance(provider, FallbackProvider) else provider.name


def _new_guide_revision(guide: Guide, resume: Resume, batch: Batch) -> GuideRevision:
    return GuideRevision(id=new_id(), guide_id=guide.id, batch_id=batch.id, revision=guide.revision, resume_revision=resume.revision, content_hash=resume.content_hash, semantic_hash=resume.semantic_hash, resume_snapshot=_resume_data(resume), plans=deepcopy(resume.plans), jd_text=batch.jd_text, status="queued", created_at=utcnow())


def process_batch_once(session_factory: Callable[[], Session], provider: Provider) -> bool:
    claim = _claim_batch(session_factory)
    if claim is None:
        return False
    jd, material, confirmed = claim.snapshot["jd"], claim.snapshot["material"], claim.snapshot["confirmed"]
    try:
        if stable_hash(claim.snapshot) != claim.input_hash:
            raise ValueError("input fingerprint mismatch")
        raw = provider.bundle(jd, material)
        try:
            content = normalize_bundle(raw, jd, material, confirmed)
        except (ValueError, TypeError):
            if not isinstance(provider, FallbackProvider) or _source(provider) == "degraded_local":
                raise
            content = normalize_bundle(LocalProvider().bundle(jd, material), jd, material, confirmed)
            provider.last_source = "degraded_local"
        with session_factory() as session:
            result = session.execute(update(Batch).where(_batch_owned(claim)).values(status="guides_pending", error_code=None, lease_until=None, claim_token=None, plans={"projects": content["plans"], "source": _source(provider)}, updated_at=utcnow()).execution_options(synchronize_session=False))
            if not result.rowcount:
                _reject_execution(session, claim, Batch)
                session.commit()
                return True
            batch = session.get(Batch, claim.batch_id)
            assert batch is not None
            for kind in ("job", "blueprint"):
                data = content["resumes"][kind]
                plans = _bound_plans(content["plans"], data["items"])
                resume = Resume(id=new_id(), batch_id=batch.id, kind=kind, title=data["title"], summary=data["summary"], header={}, items=deepcopy(data["items"]), plans=plans, content_hash=_content_hash(data, plans), semantic_hash=semantic_hash(data, plans, jd), revision=1, created_at=utcnow())
                session.add(resume)
                session.flush()
                session.add(ResumeRevision(id=new_id(), resume_id=resume.id, revision=1, title=resume.title, summary=resume.summary, header={}, items=deepcopy(resume.items), plans=deepcopy(plans), content_hash=resume.content_hash, semantic_hash=resume.semantic_hash))
                guide = Guide(id=new_id(), batch_id=batch.id, resume_id=resume.id, kind=kind, status="queued", content_hash=resume.content_hash, semantic_hash=resume.semantic_hash, revision=1, resume_revision=1, attempts=0)
                session.add(guide)
                session.flush()
                session.add(_new_guide_revision(guide, resume, batch))
            _finish_execution(session, claim.token, "succeeded")
            session.commit()
    except Exception:
        # Diagnostics deliberately contain neither user input nor upstream text.
        with session_factory() as session:
            result = session.execute(update(Batch).where(_batch_owned(claim)).values(status="failed", error_code="GENERATION_FAILED", lease_until=None, claim_token=None, updated_at=utcnow()).execution_options(synchronize_session=False))
            if result.rowcount:
                _finish_execution(session, claim.token, "failed", "GENERATION_FAILED")
            else:
                _reject_execution(session, claim, Batch)
            session.commit()
    return True


def process_guide_once(session_factory: Callable[[], Session], provider: Provider) -> bool:
    claim = _claim_guide(session_factory)
    if claim is None:
        return False
    snap = claim.snapshot
    try:
        if stable_hash(snap) != claim.input_hash:
            raise ValueError("guide input fingerprint mismatch")
        guide_resume = _guide_input(snap["resume"])
        raw = provider.guide(snap["kind"], guide_resume, deepcopy(snap["plans"]), snap["jd"])
        try:
            entries = normalize_guide(raw, snap["resume"], snap["kind"])
        except (ValueError, TypeError):
            if not isinstance(provider, FallbackProvider) or _source(provider) == "degraded_local":
                raise
            entries = normalize_guide(LocalProvider().guide(snap["kind"], _guide_input(snap["resume"]), snap["plans"], snap["jd"]), snap["resume"], snap["kind"])
            provider.last_source = "degraded_local"
        sections = {"entries": entries, "source": _source(provider)}
        with session_factory() as session:
            _lock_batch(session, claim.batch_id)
            result = session.execute(update(Guide).where(_guide_owned(claim)).values(status="ready", sections=sections, error_code=None, lease_until=None, claim_token=None).execution_options(synchronize_session=False))
            if not result.rowcount:
                _reject_execution(session, claim, Guide)
                session.commit()
                return True
            session.execute(update(GuideRevision).where(GuideRevision.guide_id == claim.resource_id, GuideRevision.revision == snap["guide_revision"]).values(status="ready", sections=sections, error_code=None, completed_at=utcnow()).execution_options(synchronize_session=False))
            _finish_execution(session, claim.token, "succeeded")
            _refresh_batch_status(session, claim.batch_id)
            session.commit()
    except Exception:
        with session_factory() as session:
            _lock_batch(session, claim.batch_id)
            result = session.execute(update(Guide).where(_guide_owned(claim)).values(status="failed", error_code="GUIDE_FAILED", lease_until=None, claim_token=None).execution_options(synchronize_session=False))
            if result.rowcount:
                session.execute(update(GuideRevision).where(GuideRevision.guide_id == claim.resource_id, GuideRevision.revision == snap["guide_revision"]).values(status="failed", error_code="GUIDE_FAILED", completed_at=utcnow()).execution_options(synchronize_session=False))
                _finish_execution(session, claim.token, "failed", "GUIDE_FAILED")
                _refresh_batch_status(session, claim.batch_id)
            else:
                _reject_execution(session, claim, Guide)
            session.commit()
    return True


def retry_batch(session_factory: Callable[[], Session], batch_id: str) -> dict:
    with session_factory() as session:
        batch = _lock_batch(session, batch_id)
        if batch is None:
            raise NotFound()
        if batch.status != "failed" or session.scalar(select(Resume.id).where(Resume.batch_id == batch_id).limit(1)):
            raise InputConflict()
        result = session.execute(update(Batch).where(Batch.id == batch_id, Batch.status == "failed", Batch.claim_token == batch.claim_token).values(status="queued", attempts=0, error_code=None, claim_token=None, lease_until=None, updated_at=utcnow()).execution_options(synchronize_session=False))
        if not result.rowcount:
            raise InputConflict()
        _finish_execution(session, batch.claim_token, "superseded", "MANUAL_RETRY")
        session.commit()
    return get_batch(session_factory, batch_id)


def retry_guide(session_factory: Callable[[], Session], batch_id: str, kind: str) -> None:
    with session_factory() as session:
        batch = _lock_batch(session, batch_id)
        guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == kind))
        if batch is None or guide is None:
            raise NotFound()
        if guide.status not in {"failed", "stale", "ready"}:
            raise InputConflict()
        resume = session.get(Resume, guide.resume_id)
        if resume is None:
            raise NotFound()
        next_revision = guide.revision + 1
        result = session.execute(update(Guide).where(Guide.id == guide.id, Guide.revision == guide.revision, Guide.status == guide.status, Guide.claim_token == guide.claim_token).values(status="queued", revision=next_revision, resume_revision=resume.revision, content_hash=resume.content_hash, semantic_hash=resume.semantic_hash, sections=None, attempts=0, error_code=None, claim_token=None, lease_until=None).execution_options(synchronize_session=False))
        if not result.rowcount:
            raise InputConflict()
        _finish_execution(session, guide.claim_token, "superseded", "MANUAL_RETRY")
        session.expire(guide)
        session.add(_new_guide_revision(guide, resume, batch))
        batch.status, batch.updated_at = "guides_pending", utcnow()
        session.commit()


_ITEM_FIELDS = {"plan_key", "source_quote", "source_status", "verification_status", "achieved", "evidence", "resources", "resource_constraints", "verification", "role", "actions", "outcome"}
_SOURCE_STATES = {"none", "user_confirmed", "user_unconfirmed", "ai_generated"}
_VERIFY_STATES = {"user_confirmed", "user_unconfirmed", "ai_generated"}


def _clean_items(resume: Resume, items: list[dict], material: str) -> list[dict]:
    if not items or len(items) > 20 or not all(isinstance(item, dict) for item in items):
        raise ValueError("invalid items")
    input_ids = [item.get("id") for item in items if item.get("id")]
    if len(set(input_ids)) != len(input_ids):
        raise ValueError("duplicate item id")
    original = {item["id"]: item for item in resume.items}
    locked_positions = {index: item["id"] for index, item in enumerate(resume.items) if item.get("locked")}
    if any(index >= len(items) or items[index].get("id") != item_id for index, item_id in locked_positions.items()):
        raise InputConflict()
    cleaned = []
    for item in items:
        old = original.get(item.get("id"))
        body = str(item.get("text", "")).strip()
        if len(body) < 4 or len(body) > 600:
            raise ValueError("invalid item text")
        source = deepcopy(old) if old else {"id": new_id(), "plan_key": "user-project-" + new_id()[:12], "source_quote": "", "source_status": "none", "verification_status": "user_unconfirmed", "achieved": False}
        # Conservative defaults also let prototype snapshots with missing JSON
        # fields be edited without pretending that their provenance is known.
        source.setdefault("plan_key", "user-project-" + new_id()[:12])
        source.setdefault("source_quote", "")
        source.setdefault("source_status", "none")
        source.setdefault("verification_status", "user_unconfirmed")
        source.setdefault("achieved", False)
        text_changed = old is not None and body != old["text"]
        source["text"] = body
        for field in _ITEM_FIELDS:
            if field in item:
                if field == "plan_key" and not old and not item[field]:
                    continue
                source[field] = deepcopy(item[field])
        if text_changed:
            source["verification_status"] = "user_unconfirmed"
            source["achieved"] = False
        explicit_confirmation = item.get("confirm") is True
        if explicit_confirmation:
            source["verification_status"] = "user_confirmed"
            source["source_status"] = "user_confirmed"
            source["achieved"] = item.get("achieved", source["achieved"])
        elif item.get("confirm") is False:
            if source["verification_status"] == "user_confirmed":
                source["verification_status"] = "user_unconfirmed"
                source["source_status"] = "user_unconfirmed"
            if item.get("achieved") is True:
                raise ValueError("completion requires explicit confirmation")
            source["achieved"] = False
        if not explicit_confirmation and ((source.get("verification_status") == "user_confirmed" and (not old or old.get("verification_status") != "user_confirmed")) or (source.get("source_status") == "user_confirmed" and (not old or old.get("source_status") != "user_confirmed")) or (source.get("achieved") is True and (not old or not old.get("achieved")))):
            raise ValueError("explicit item confirmation required")
        if source.get("source_status") not in _SOURCE_STATES or source.get("verification_status") not in _VERIFY_STATES or not isinstance(source.get("achieved"), bool):
            raise ValueError("invalid provenance")
        if not isinstance(source.get("source_quote"), str) or (source["source_quote"] and source["source_quote"] not in material):
            raise ValueError("invalid source quote")
        if not isinstance(source.get("plan_key"), str) or not 1 <= len(source["plan_key"]) <= 40:
            raise ValueError("invalid plan reference")
        if "locked" in item or "locked" in source:
            source["locked"] = bool(item.get("locked", source.get("locked", False)))
        if old and old.get("locked"):
            if old["text"] != source["text"] or old.get("plan_key") != source.get("plan_key"):
                raise InputConflict()
        cleaned.append(source)
    return cleaned


def _sync_plans(old_plans: list[dict], original: list[dict], items: list[dict], supplied: list[dict] | None) -> list[dict]:
    if supplied is not None and (not isinstance(supplied, list) or len(supplied) > 20 or not all(isinstance(plan, dict) for plan in supplied)):
        raise ValueError("invalid plans")
    plans = deepcopy(supplied if supplied is not None else old_plans)
    if any(not isinstance(plan.get("key"), str) or not 1 <= len(plan["key"]) <= 40 for plan in plans):
        raise ValueError("invalid plan key")
    if len({plan["key"] for plan in plans}) != len(plans):
        raise ValueError("duplicate plan key")
    plan_map = {plan["key"]: plan for plan in plans}
    old_map = {plan["key"]: plan for plan in old_plans}
    original_map = {item["id"]: item for item in original}
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["plan_key"], []).append(item)
    result = []
    for key, members in grouped.items():
        plan = plan_map.get(key)
        newly_created = plan is None
        if newly_created:
            plan = {"key": key, "title": "用户新增项目", "target_evidence": "", "target_evidence_status": "user_unconfirmed", "background": "项目背景与约束待用户补充。", "task": "本人职责待用户补充和确认。", "actions": [item["text"] for item in members], "outcome": "实际结果与预期产物待用户补充和确认。", "verification": "验证材料和检查方式待用户补充。"}
        old_member_ids = {item["id"] for item in original if item.get("plan_key") == key}
        text_or_membership_changed = old_member_ids != {item["id"] for item in members} or any(not original_map.get(item["id"]) or original_map[item["id"]]["text"] != item["text"] or original_map[item["id"]].get("plan_key") != key for item in members)
        # A plain text edit must not feed the previous project's methods/results to
        # the next guide. Explicitly edited plans remain the user's own new draft.
        if text_or_membership_changed and (supplied is None or plan == old_map.get(key)) and not newly_created:
            plan["task"] = next((str(item["role"]) for item in members if item.get("role")), "本人职责以当前修订条目为准，待用户确认。")
            plan["actions"] = [item["text"] for item in members]
            plan["outcome"] = next((str(item["outcome"]) for item in members if item.get("outcome")), "当前修订未单独确认结果；实际结果与预期产物需分别核对。")
        if newly_created or text_or_membership_changed or plan != old_map.get(key) or any({k: v for k, v in item.items() if k != "locked"} != {k: v for k, v in original_map.get(item["id"], {}).items() if k != "locked"} for item in members):
            plan["revision_items"] = [{k: deepcopy(v) for k, v in item.items() if k != "locked"} for item in members]
        for field in ("title", "background", "task", "outcome", "verification"):
            if not isinstance(plan.get(field), str) or not plan[field].strip():
                raise ValueError("incomplete plan")
        if not isinstance(plan.get("actions"), list) or not plan["actions"] or not all(isinstance(action, str) and action.strip() for action in plan["actions"]):
            raise ValueError("invalid plan actions")
        result.append(plan)
    return result


def save_resume(session_factory: Callable[[], Session], batch_id: str, kind: str, *, revision: int, title: str, summary: str, items: list[dict], plans: list[dict] | None = None, header: dict | None = None) -> dict:
    with session_factory() as session:
        batch = _lock_batch(session, batch_id)
        resume = session.scalar(select(Resume).where(Resume.batch_id == batch_id, Resume.kind == kind))
        if batch is None or resume is None:
            raise NotFound()
        if resume.revision != revision:
            raise InputConflict()
        saved_header = deepcopy(resume.header) if header is None else _clean_header(header)
        cleaned = _clean_items(resume, items, batch.material_text)
        snapshot_plans = _sync_plans(resume.plans, resume.items, cleaned, plans)
        title, summary = title.strip(), summary.strip()
        if not (2 <= len(title) <= 160 and 4 <= len(summary) <= 1000):
            raise ValueError("invalid heading")
        data = {"title": title, "summary": summary, "header": saved_header, "items": cleaned}
        content_hash = _content_hash(data, snapshot_plans)
        fingerprint = semantic_hash(data, snapshot_plans, batch.jd_text)
        changed = content_hash != resume.content_hash
        next_revision = revision + 1 if changed else revision
        result = session.execute(update(Resume).where(Resume.id == resume.id, Resume.revision == revision).values(revision=next_revision, title=title, summary=summary, header=saved_header, items=cleaned, plans=snapshot_plans, content_hash=content_hash, semantic_hash=fingerprint).execution_options(synchronize_session=False))
        if not result.rowcount:
            raise InputConflict()
        if changed:
            session.add(ResumeRevision(id=new_id(), resume_id=resume.id, revision=next_revision, title=title, summary=summary, header=deepcopy(saved_header), items=deepcopy(cleaned), plans=deepcopy(snapshot_plans), content_hash=content_hash, semantic_hash=fingerprint))
            if fingerprint != resume.semantic_hash:
                guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == kind))
                if guide:
                    _finish_execution(session, guide.claim_token, "superseded", "RESUME_CHANGED")
                    old_guide_revision = _guide_revision(session, guide)
                    if old_guide_revision.status in {"queued", "running"}:
                        old_guide_revision.status, old_guide_revision.error_code, old_guide_revision.completed_at = "superseded", "RESUME_CHANGED", utcnow()
                    session.execute(update(Guide).where(Guide.id == guide.id).values(status="stale", claim_token=None, lease_until=None, error_code=None).execution_options(synchronize_session=False))
                session.flush()
                _refresh_batch_status(session, batch_id)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise InputConflict() from None
    return get_batch(session_factory, batch_id)


def list_revisions(session_factory: Callable[[], Session], batch_id: str, kind: str) -> list[dict]:
    with session_factory() as session:
        resume = session.scalar(select(Resume).where(Resume.batch_id == batch_id, Resume.kind == kind))
        if not resume:
            raise NotFound()
        rows = session.scalars(select(ResumeRevision).where(ResumeRevision.resume_id == resume.id).order_by(ResumeRevision.revision.desc())).all()
        return [{"revision": row.revision, "created_at": row.created_at.isoformat(), "title": row.title, "summary": row.summary, "header": deepcopy(row.header), "items": row.items, "plans": row.plans, "content_hash": row.content_hash, "semantic_hash": row.semantic_hash} for row in rows]


def get_resume_revision(session_factory: Callable[[], Session], batch_id: str, kind: str, revision: int) -> dict:
    for row in list_revisions(session_factory, batch_id, kind):
        if row["revision"] == revision:
            return row
    raise NotFound()


def list_guide_revisions(session_factory: Callable[[], Session], batch_id: str, kind: str) -> list[dict]:
    with session_factory() as session:
        guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == kind))
        if guide is None:
            raise NotFound()
        rows = session.scalars(select(GuideRevision).where(GuideRevision.guide_id == guide.id).order_by(GuideRevision.revision.desc())).all()
        return [{"id": row.id, "revision": row.revision, "resume_revision": row.resume_revision, "content_hash": row.content_hash, "semantic_hash": row.semantic_hash, "resume": row.resume_snapshot, "plans": row.plans, "jd_text": row.jd_text, "status": row.status, "source": row.sections.get("source") if row.sections else None, "entries": row.sections.get("entries", []) if row.sections else [], "error_code": row.error_code, "created_at": row.created_at.isoformat(), "completed_at": row.completed_at.isoformat() if row.completed_at else None} for row in rows]


def get_guide_revision(session_factory: Callable[[], Session], batch_id: str, kind: str, revision: int) -> dict:
    for row in list_guide_revisions(session_factory, batch_id, kind):
        if row["revision"] == revision:
            return row
    raise NotFound()


def list_executions(session_factory: Callable[[], Session], batch_id: str) -> list[dict]:
    with session_factory() as session:
        if session.get(Batch, batch_id) is None:
            raise NotFound()
        rows = session.scalars(select(TaskExecution).where(TaskExecution.batch_id == batch_id).order_by(TaskExecution.started_at, TaskExecution.id)).all()
        # Full frozen inputs stay private in the DB, never in diagnostic listings.
        return [{"id": row.id, "task_type": row.task_type, "guide_id": row.guide_id, "attempt": row.attempt, "input_hash": row.input_hash, "status": row.status, "error_code": row.error_code, "started_at": row.started_at.isoformat(), "finished_at": row.finished_at.isoformat() if row.finished_at else None} for row in rows]


def get_batch(session_factory: Callable[[], Session], batch_id: str) -> dict:
    with session_factory() as session:
        batch = session.get(Batch, batch_id)
        if not batch:
            raise NotFound()
        return serialize_batch(batch)


def latest_batch(session_factory: Callable[[], Session]) -> dict | None:
    with session_factory() as session:
        batch = session.scalar(select(Batch).order_by(Batch.created_at.desc()).limit(1))
        return serialize_batch(batch) if batch else None


def list_batches(session_factory: Callable[[], Session], limit: int = 20) -> list[dict]:
    with session_factory() as session:
        rows = session.scalars(select(Batch).order_by(Batch.created_at.desc()).limit(limit)).all()
        return [{"id": row.id, "status": row.status, "created_at": row.created_at.isoformat(), "jd_excerpt": row.jd_text[:60]} for row in rows]


def serialize_batch(batch: Batch) -> dict:
    resumes = {resume.kind: resume for resume in batch.resumes}
    return {
        "id": batch.id, "status": batch.status, "error_code": batch.error_code,
        "jd_text": batch.jd_text, "material_text": batch.material_text, "material_confirmed": batch.material_confirmed,
        "created_at": batch.created_at.isoformat(),
        "source": batch.plans.get("source") if batch.plans else None,
        "unverified_targets": [plan["target_inference"] for plan in batch.plans.get("projects", []) if plan.get("target_evidence_status") == "ai_inferred"] if batch.plans else [],
        "resumes": {resume.kind: {"id": resume.id, **_resume_data(resume), "plans": deepcopy(resume.plans), "content_hash": resume.content_hash, "semantic_hash": resume.semantic_hash, "revision": resume.revision} for resume in batch.resumes},
        "guides": {guide.kind: {"status": guide.status, "revision": guide.revision, "resume_revision": guide.resume_revision, "applies_to_revision": resumes[guide.kind].revision if guide.status == "ready" and guide.semantic_hash == resumes[guide.kind].semantic_hash else None, "content_hash": guide.content_hash, "semantic_hash": guide.semantic_hash, "source": guide.sections.get("source") if guide.sections else None, "entries": guide.sections.get("entries", []) if guide.sections else [], "error_code": guide.error_code} for guide in batch.guides},
    }
