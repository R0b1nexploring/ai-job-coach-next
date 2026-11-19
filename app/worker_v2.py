"""Personal-scope v2 batch and guide workers with journal/lease fencing.

Calls to providers occur outside the transaction. This is an S1 worker: it
persists a single final draft, not resumable generation phases or file QA.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import httpx
from sqlalchemy import and_, or_, select, update

from app.content_v2 import FactSnapshot as FactContract, ResumeDocumentV2
from app.generation_v2 import build_writer_v2, generate_bundle_v2, generate_guide_v2, review_bundle_v2
from app.guide_v2 import content_hash, coverage_units, guide_input_hash, semantic_hash
from app.models import Batch, FactSnapshot, Guide, GuideRevision, Resume, ResumeRevision, TaskExecution, new_id, utcnow
from app.privacy_v2 import V2Deleted, v2_write_transaction

LEASE_SECONDS = 900
MAX_ATTEMPTS = 3


def _ids(session_factory, model):
    # The global journal lock makes the initial candidate scan safe; every
    # selected batch is rechecked under its own guarded transaction.
    now = utcnow()
    with v2_write_transaction(session_factory) as session:
        deleted = session.info["v2_journal_guard"]["deleted_ids"]
        if model is Batch:
            rows = session.execute(select(Batch.id).where(Batch.schema_version == 2,
                or_(Batch.status == "queued", and_(Batch.status == "running", or_(Batch.lease_until.is_(None), Batch.lease_until <= now))))
                .order_by(Batch.created_at, Batch.id)).scalars().all()
            return [identifier for identifier in rows if identifier not in deleted]
        rows = session.execute(select(Guide.batch_id, Guide.id).join(Batch, Batch.id == Guide.batch_id).where(
            Batch.schema_version == 2, Guide.schema_version == 2,
            or_(Guide.status == "queued", and_(Guide.status == "running", or_(Guide.lease_until.is_(None), Guide.lease_until <= now))))
            .order_by(Batch.created_at, Guide.kind, Guide.id)).all()
        return [(batch_id, guide_id) for batch_id, guide_id in rows if batch_id not in deleted]


def _close_execution(session, token, status, code):
    if token:
        session.execute(update(TaskExecution).where(TaskExecution.claim_token == token,
            TaskExecution.status == "running").values(status=status, error_code=code, finished_at=utcnow()))


def _claim_batch(session_factory):
    now = utcnow()
    for batch_id in _ids(session_factory, Batch):
        try:
            with v2_write_transaction(session_factory, batch_id=batch_id) as session:
                batch = session.scalar(select(Batch).where(Batch.id == batch_id, Batch.schema_version == 2).with_for_update())
                if not batch or batch.status not in {"queued", "running"}:
                    continue
                expired = or_(Batch.lease_until.is_(None), Batch.lease_until <= now)
                eligible = or_(Batch.status == "queued", and_(Batch.status == "running", expired))
                if batch.attempts >= MAX_ATTEMPTS:
                    result = session.execute(update(Batch).where(Batch.id == batch_id, Batch.schema_version == 2, eligible,
                        Batch.claim_token == batch.claim_token).values(status="failed", error_code="ATTEMPTS_EXHAUSTED", claim_token=None, lease_until=None).execution_options(synchronize_session=False))
                    if result.rowcount:
                        _close_execution(session, batch.claim_token, "lease_expired", "ATTEMPTS_EXHAUSTED")
                    continue
                snapshot = session.get(FactSnapshot, batch.fact_snapshot_id) if batch.fact_snapshot_id else None
                if not snapshot or snapshot.scope_id != "local":
                    batch.status, batch.error_code = "failed", "SNAPSHOT_UNAVAILABLE"
                    continue
                token = new_id()
                old_token = batch.claim_token
                result = session.execute(update(Batch).where(Batch.id == batch_id, Batch.schema_version == 2, eligible,
                    Batch.attempts == batch.attempts, Batch.claim_token == old_token).values(status="running", attempts=batch.attempts + 1,
                    claim_token=token, lease_until=now + timedelta(seconds=LEASE_SECONDS), error_code=None, updated_at=now).execution_options(synchronize_session=False))
                if not result.rowcount:
                    continue
                _close_execution(session, old_token, "lease_expired", "LEASE_EXPIRED")
                frozen = {"batch_id": batch.id, "token": token, "input_hash": batch.generation_input_hash,
                    "fact_snapshot_id": snapshot.id, "facts": deepcopy(snapshot.claims), "source_copies": deepcopy(snapshot.source_copies),
                    "jd": batch.jd_text, "target": deepcopy(batch.target_snapshot), "profile": deepcopy(batch.profile or {}),
                    "resume_locale": batch.resume_locale, "guide_locale": batch.guide_locale,
                    "configuration": deepcopy((batch.generation_preferences or {}).get("provider_configuration"))}
                session.add(TaskExecution(batch_id=batch.id, task_type="batch_v2", claim_token=token, attempt=batch.attempts + 1,
                    input_hash=batch.generation_input_hash or "", input_snapshot={"schema_version": 2, "fact_snapshot_id": snapshot.id}, status="running"))
                return frozen
        except V2Deleted:
            continue
    return None


def _owned_batch(session, claim):
    batch = session.scalar(select(Batch).where(Batch.id == claim["batch_id"], Batch.schema_version == 2,
        Batch.status == "running", Batch.claim_token == claim["token"], Batch.lease_until > utcnow(),
        Batch.generation_input_hash == claim["input_hash"], Batch.fact_snapshot_id == claim["fact_snapshot_id"]).with_for_update())
    if not batch:
        return None
    snapshot = session.get(FactSnapshot, claim["fact_snapshot_id"])
    if (not snapshot or snapshot.scope_id != "local" or snapshot.claims != claim["facts"]
        or snapshot.source_copies != claim["source_copies"] or batch.jd_text != claim["jd"]
        or batch.target_snapshot != claim["target"] or (batch.profile or {}) != claim["profile"]
        or batch.resume_locale != claim["resume_locale"] or batch.guide_locale != claim["guide_locale"]
        or (batch.generation_preferences or {}).get("provider_configuration") != claim["configuration"]):
        return None
    return batch


def _provider_failure(exc, fallback):
    """Persist only a closed category; never expose responses, URLs or inputs."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return "MODEL_ACCESS_DENIED"
        if status == 429:
            return "MODEL_RATE_LIMITED"
        if status >= 500:
            return "MODEL_UNAVAILABLE"
    if isinstance(exc, httpx.TimeoutException):
        return "MODEL_TIMED_OUT"
    if isinstance(exc, httpx.NetworkError):
        return "MODEL_UNAVAILABLE"
    return fallback


def _fail_batch(session_factory, claim, code):
    try:
        with v2_write_transaction(session_factory, batch_id=claim["batch_id"]) as session:
            batch = _owned_batch(session, claim)
            if batch:
                batch.status, batch.error_code, batch.claim_token, batch.lease_until = "failed", code, None, None
                batch.updated_at = utcnow()
                _close_execution(session, claim["token"], "failed", code)
            else:
                _close_execution(session, claim["token"], "superseded", "OWNERSHIP_LOST")
    except V2Deleted:
        pass


def process_batch_v2_once(session_factory, provider_factory=build_writer_v2):
    claim = _claim_batch(session_factory)
    if claim is None:
        return False
    try:
        if not isinstance(claim["configuration"], dict):
            raise ValueError("frozen provider configuration missing")
        provider = provider_factory(claim["configuration"])
        facts = FactContract.model_validate({"snapshot_id": claim["fact_snapshot_id"], "private_scope": "local", "claims": claim["facts"]})
        bundle = generate_bundle_v2(provider, jd_text=claim["jd"], snapshot=facts,
            private_header=claim["profile"], resume_locale=claim["resume_locale"])
        review = review_bundle_v2(provider, result=bundle, jd_text=claim["jd"], snapshot=facts,
            private_header=claim["profile"], resume_locale=claim["resume_locale"])
        with v2_write_transaction(session_factory, batch_id=claim["batch_id"]) as session:
            batch = _owned_batch(session, claim)
            if not batch:
                _close_execution(session, claim["token"], "superseded", "OWNERSHIP_LOST")
                return True
            batch.plans = {"projects": bundle["plans"], "v2_review": review,
                           "candidate": bundle["candidate"], "id_map": bundle["id_map"], "source": bundle["source"]}
            batch.claim_token, batch.lease_until, batch.error_code = None, None, None
            batch.updated_at = utcnow()
            if review["status"] == "blocked":
                # Private diagnostic only: preserve reviewer target IDs without
                # creating deliverable Resume or Guide rows.
                batch.plans = {**batch.plans, "diagnostic_documents": deepcopy(bundle["documents"])}
                batch.status = "review_blocked"
                _close_execution(session, claim["token"], "blocked", "CONTENT_REVIEW_BLOCKED")
                return True
            frozen_facts = {"snapshot_id": claim["fact_snapshot_id"], "claims": claim["facts"], "source_copies": claim["source_copies"]}
            for kind, document in bundle["documents"].items():
                plans = deepcopy(bundle["plans"])
                header = deepcopy(claim["profile"])
                chash = content_hash(document, header, {"plans": plans, "fact_snapshot_id": claim["fact_snapshot_id"], "target": claim["target"]})
                shash = semantic_hash(document, claim["target"], frozen_facts, plans)
                resume = Resume(id=new_id(), batch_id=batch.id, kind=kind, title=document["title"], summary=document["summary"]["text"],
                    header=header, items=[], plans=plans, content_hash=chash, semantic_hash=shash, revision=1, schema_version=2, document_v2=document)
                session.add(resume)
                session.flush()
                session.add(ResumeRevision(resume_id=resume.id, revision=1, title=resume.title, summary=resume.summary,
                    header=deepcopy(header), items=[], plans=deepcopy(plans), content_hash=chash, semantic_hash=shash,
                    schema_version=2, document_v2=deepcopy(document)))
                coverage = coverage_units(document)
                ghash = guide_input_hash(shash, claim["guide_locale"], coverage, "v2-s1")
                guide = Guide(id=new_id(), batch_id=batch.id, resume_id=resume.id, kind=kind, status="queued", content_hash=chash,
                    semantic_hash=shash, revision=1, resume_revision=1, schema_version=2, guide_locale=claim["guide_locale"], guide_input_hash=ghash)
                session.add(guide)
                session.flush()
                session.add(GuideRevision(guide_id=guide.id, batch_id=batch.id, revision=1, resume_revision=1, content_hash=chash,
                    semantic_hash=shash, resume_snapshot=deepcopy(document), plans=plans, jd_text=claim["jd"], status="queued", schema_version=2,
                    guide_locale=claim["guide_locale"], guide_input_hash=ghash, coverage_unit_ids=coverage,
                    v2_snapshot={"facts": frozen_facts, "target": claim["target"], "provider_configuration": claim["configuration"]}))
            batch.status = "guides_pending"
            _close_execution(session, claim["token"], "succeeded", None)
    except V2Deleted:
        pass
    except Exception as exc:
        _fail_batch(session_factory, claim, _provider_failure(exc, "GENERATION_FAILED"))
    return True


def _guide_input_matches(guide, revision, saved) -> bool:
    """Compare the persisted generation binding against today's rule set."""
    if not revision or not saved or not isinstance(revision.coverage_unit_ids, list):
        return False
    try:
        document = ResumeDocumentV2.model_validate(revision.resume_snapshot).model_dump(mode="json")
        current_coverage = coverage_units(document)
        expected_hash = guide_input_hash(revision.semantic_hash, revision.guide_locale, current_coverage, "v2-s1")
        return (revision.resume_revision == guide.resume_revision
                and revision.semantic_hash == guide.semantic_hash == saved.semantic_hash
                and revision.content_hash == guide.content_hash == saved.content_hash
                and revision.guide_locale == guide.guide_locale
                and revision.resume_snapshot == saved.document_v2
                and revision.coverage_unit_ids == current_coverage
                and revision.guide_input_hash == guide.guide_input_hash == expected_hash)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def _mark_guide_input_stale(session, batch, guide, revision) -> None:
    """Keep the frozen input intact and require a new guide run."""
    old_token = guide.claim_token
    guide.status, guide.error_code, guide.claim_token, guide.lease_until = "stale", "GUIDE_INPUT_STALE", None, None
    if revision and revision.status in {"queued", "running"}:
        revision.status, revision.error_code, revision.completed_at = "stale", "GUIDE_INPUT_STALE", utcnow()
    batch.status, batch.updated_at = "guides_partial", utcnow()
    _close_execution(session, old_token, "superseded", "GUIDE_INPUT_STALE")


def _claim_guide(session_factory):
    now = utcnow()
    for batch_id, guide_id in _ids(session_factory, Guide):
        try:
            with v2_write_transaction(session_factory, batch_id=batch_id) as session:
                batch = session.scalar(select(Batch).where(Batch.id == batch_id, Batch.schema_version == 2).with_for_update())
                guide = session.scalar(select(Guide).where(Guide.id == guide_id, Guide.batch_id == batch_id, Guide.schema_version == 2).with_for_update())
                if not batch or not guide:
                    continue
                expired = or_(Guide.lease_until.is_(None), Guide.lease_until <= now)
                eligible = or_(Guide.status == "queued", and_(Guide.status == "running", expired))
                if not session.scalar(select(Guide.id).where(Guide.id == guide.id, eligible)):
                    continue
                revision = session.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id,
                    GuideRevision.revision == guide.revision, GuideRevision.schema_version == 2))
                resume = session.get(Resume, guide.resume_id)
                saved = session.scalar(select(ResumeRevision).where(ResumeRevision.resume_id == guide.resume_id,
                    ResumeRevision.revision == guide.resume_revision, ResumeRevision.schema_version == 2))
                if (not revision or not resume or not saved or resume.schema_version != 2 or not revision.v2_snapshot
                        or resume.semantic_hash != guide.semantic_hash or not _guide_input_matches(guide, revision, saved)):
                    _mark_guide_input_stale(session, batch, guide, revision)
                    continue
                old_token = guide.claim_token
                if guide.attempts >= MAX_ATTEMPTS:
                    result = session.execute(update(Guide).where(Guide.id == guide.id, Guide.schema_version == 2, eligible,
                        Guide.claim_token == old_token).values(status="failed", error_code="ATTEMPTS_EXHAUSTED", claim_token=None, lease_until=None).execution_options(synchronize_session=False))
                    if result.rowcount:
                        revision.status, revision.error_code, revision.completed_at = "failed", "ATTEMPTS_EXHAUSTED", now
                        _close_execution(session, old_token, "lease_expired", "ATTEMPTS_EXHAUSTED")
                        session.flush()
                        _refresh_guide_status(session, batch)
                    continue
                token = new_id()
                result = session.execute(update(Guide).where(Guide.id == guide.id, Guide.schema_version == 2, eligible,
                    Guide.revision == guide.revision, Guide.claim_token == old_token, Guide.attempts == guide.attempts)
                    .values(status="running", attempts=guide.attempts + 1, claim_token=token,
                            lease_until=now + timedelta(seconds=LEASE_SECONDS), error_code=None).execution_options(synchronize_session=False))
                if not result.rowcount:
                    continue
                _close_execution(session, old_token, "lease_expired", "LEASE_EXPIRED")
                revision.status = "running"
                frozen = {"batch_id": batch.id, "guide_id": guide.id, "token": token, "guide_revision": guide.revision,
                    "resume_id": guide.resume_id, "resume_revision": guide.resume_revision, "semantic_hash": guide.semantic_hash, "content_hash": guide.content_hash,
                    "guide_input_hash": guide.guide_input_hash, "coverage_unit_ids": deepcopy(revision.coverage_unit_ids),
                    "document": deepcopy(revision.resume_snapshot), "jd": revision.jd_text,
                    "header": deepcopy(saved.header or {}), "locale": revision.guide_locale,
                    "facts": deepcopy(revision.v2_snapshot.get("facts")), "plans": deepcopy(revision.plans),
                    "configuration": deepcopy(revision.v2_snapshot.get("provider_configuration"))}
                session.add(TaskExecution(batch_id=batch.id, guide_id=guide.id, task_type="guide_v2", claim_token=token,
                    attempt=guide.attempts + 1, input_hash=guide.guide_input_hash or "", input_snapshot={"schema_version": 2,
                        "resume_id": guide.resume_id, "resume_revision": guide.resume_revision, "guide_revision": guide.revision}, status="running"))
                return frozen
        except V2Deleted:
            continue
    return None


def _owned_guide(session, claim):
    batch = session.scalar(select(Batch).where(Batch.id == claim["batch_id"], Batch.schema_version == 2).with_for_update())
    if not batch:
        return None, None, None
    guide = session.scalar(select(Guide).where(Guide.id == claim["guide_id"], Guide.schema_version == 2,
        Guide.status == "running", Guide.revision == claim["guide_revision"], Guide.resume_revision == claim["resume_revision"],
        Guide.semantic_hash == claim["semantic_hash"], Guide.content_hash == claim["content_hash"], Guide.guide_input_hash == claim["guide_input_hash"],
        Guide.claim_token == claim["token"], Guide.lease_until > utcnow()).with_for_update())
    if not guide:
        return None, None, None
    revision = session.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id,
        GuideRevision.revision == claim["guide_revision"], GuideRevision.schema_version == 2,
        GuideRevision.resume_revision == claim["resume_revision"], GuideRevision.semantic_hash == claim["semantic_hash"], GuideRevision.content_hash == claim["content_hash"],
        GuideRevision.guide_input_hash == claim["guide_input_hash"]))
    resume = session.get(Resume, claim["resume_id"])
    saved = session.scalar(select(ResumeRevision).where(ResumeRevision.resume_id == claim["resume_id"],
        ResumeRevision.revision == claim["resume_revision"], ResumeRevision.schema_version == 2))
    if revision and saved and not _guide_input_matches(guide, revision, saved):
        _mark_guide_input_stale(session, batch, guide, revision)
        return None, None, None
    if (not revision or not resume or resume.schema_version != 2 or not saved
        or resume.semantic_hash != claim["semantic_hash"] or saved.semantic_hash != claim["semantic_hash"] or saved.content_hash != claim["content_hash"]
        or revision.resume_snapshot != claim["document"] or saved.document_v2 != claim["document"]
        or revision.jd_text != claim["jd"] or revision.guide_locale != claim["locale"]
        or (saved.header or {}) != claim["header"]
        or revision.plans != claim["plans"] or saved.plans != claim["plans"]
        or (revision.v2_snapshot or {}).get("facts") != claim["facts"]
        or (revision.v2_snapshot or {}).get("provider_configuration") != claim["configuration"]):
        return None, None, None
    return batch, guide, revision


def _refresh_guide_status(session, batch):
    statuses = session.scalars(select(Guide.status).where(Guide.batch_id == batch.id, Guide.schema_version == 2)).all()
    if len(statuses) == 2 and all(status == "ready" for status in statuses):
        batch.status = "ready"
    elif any(status in {"failed", "stale"} for status in statuses):
        batch.status = "guides_partial"
    else:
        batch.status = "guides_pending"
    batch.updated_at = utcnow()


def process_guide_v2_once(session_factory, provider_factory=build_writer_v2):
    claim = _claim_guide(session_factory)
    if claim is None:
        return False
    try:
        if not isinstance(claim["configuration"], dict):
            raise ValueError("frozen provider configuration missing")
        provider = provider_factory(claim["configuration"])
        frozen_facts = claim["facts"]
        if not isinstance(frozen_facts, dict) or not isinstance(frozen_facts.get("source_copies"), list):
            raise ValueError("frozen guide fact snapshot missing")
        snapshot = FactContract.model_validate({"snapshot_id": frozen_facts["snapshot_id"],
            "private_scope": "local", "claims": frozen_facts["claims"]})
        source_types = {}
        for source in frozen_facts["source_copies"]:
            source_id, source_type = source["revision_id"], source["source_type"]
            if source_id in source_types:
                raise ValueError("duplicate frozen source revision")
            source_types[source_id] = source_type
        result = generate_guide_v2(provider, document=claim["document"], resume_id=claim["resume_id"],
            resume_revision=claim["resume_revision"], semantic_hash=claim["semantic_hash"], jd_text=claim["jd"],
            private_header=claim["header"], guide_locale=claim["locale"], fact_snapshot=snapshot,
            source_types=source_types, plans=claim["plans"])
        with v2_write_transaction(session_factory, batch_id=claim["batch_id"]) as session:
            batch, guide, revision = _owned_guide(session, claim)
            if not guide:
                _close_execution(session, claim["token"], "superseded", "OWNERSHIP_LOST")
                return True
            if result.get("coverage_unit_ids") != claim["coverage_unit_ids"]:
                _mark_guide_input_stale(session, batch, guide, revision)
                return True
            guide.sections = result
            guide.status, guide.error_code, guide.claim_token, guide.lease_until = "ready", None, None, None
            revision.sections, revision.status, revision.error_code, revision.completed_at = deepcopy(result), "ready", None, utcnow()
            _close_execution(session, claim["token"], "succeeded", None)
            session.flush()
            _refresh_guide_status(session, batch)
    except V2Deleted:
        pass
    except Exception as exc:
        code = _provider_failure(exc, "GUIDE_FAILED")
        try:
            with v2_write_transaction(session_factory, batch_id=claim["batch_id"]) as session:
                batch, guide, revision = _owned_guide(session, claim)
                if guide:
                    guide.status, guide.error_code, guide.claim_token, guide.lease_until = "failed", code, None, None
                    revision.status, revision.error_code, revision.completed_at = "failed", code, utcnow()
                    _close_execution(session, claim["token"], "failed", code)
                    session.flush()
                    _refresh_guide_status(session, batch)
                else:
                    _close_execution(session, claim["token"], "superseded", "OWNERSHIP_LOST")
        except V2Deleted:
            pass
    return True
