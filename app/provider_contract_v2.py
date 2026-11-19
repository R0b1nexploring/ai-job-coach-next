"""Bounded v2 writer contracts; structural checks do not establish truth.

This module performs no network calls and does not publish documents. Inputs to
these interfaces must first be prepared by a trusted privacy projection; raw
uploads, evidence copies, headers, credentials and arbitrary provider config do
not belong here. Validation does not establish that projection is complete.
"""
from __future__ import annotations

from typing import Annotated, Literal, Protocol
import json
import re
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from app.guide_v2 import validate_guide_coverage
from app.content_v2 import DisplayAlias, FactSnapshot, validate_entry_claim_types, validate_entry_display_aliases, validate_display_alias_values, is_explicit_year_range
from app.model_projection_v2 import MODEL_FACT_TYPES, _contains_contact, contains_sensitive_identifier, project_facts_with_diagnostics

Locale = Literal["zh-CN", "en"]
Kind = Literal["job", "blueprint"]
Key = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]
ShortText = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WriterFact(Contract):
    claim_id: Key
    fact_type: Annotated[str, Field(min_length=1, max_length=80)]
    value: Annotated[str, Field(max_length=2000)] | None
    confirmation: Literal["unconfirmed", "user_confirmed", "disputed", "unknown"]
    completion: Literal["completed", "in_progress", "planned", "unknown", "not_applicable"]
    conflict_ids: tuple[Key, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def known_safe_shape(self):
        if self.fact_type not in MODEL_FACT_TYPES:
            raise ValueError("unsupported model fact type")
        if contains_sensitive_identifier(self.claim_id, ()) or any(contains_sensitive_identifier(ref, ()) for ref in self.conflict_ids):
            raise ValueError("fact identifier requires privacy review")
        if self.value and _contains_contact(self.value, ()):
            raise ValueError("fact requires privacy review")
        return self


class WriterRequest(Contract):
    # Deliberately not the full FactSnapshot: no source text or header fields.
    jd_text: Annotated[str, Field(min_length=1, max_length=15000)]
    facts: tuple[WriterFact, ...] = Field(max_length=200)
    resume_locale: Locale
    _alias_to_claim: dict[str, str] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def unique_facts(self):
        if _contains_contact(self.jd_text, ()):
            raise ValueError("JD requires privacy review")
        ids = [fact.claim_id for fact in self.facts]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate claim id")
        if any(not set(fact.conflict_ids) <= set(ids) for fact in self.facts):
            raise ValueError("unknown conflict id")
        return self


def build_writer_request(*, jd_text: str, snapshot: FactSnapshot, private_header: dict,
                         resume_locale: Locale, additional_private_values: tuple[str, ...] = ()) -> WriterRequest:
    """Build from allowlisted facts; reject identifiable JD instead of guessing edits."""
    private_values = tuple(value.strip() for value in (*private_header.values(), *additional_private_values)
                           if isinstance(value, str) and value.strip())
    if _contains_contact(jd_text, private_values):
        raise ValueError("JD requires privacy review")
    projected = project_facts_with_diagnostics(snapshot, private_header=private_header,
        additional_private_values=additional_private_values, alias_identifiers=True)
    request = WriterRequest(jd_text=jd_text, facts=tuple(projected.model_input["claims"]), resume_locale=resume_locale)
    request._alias_to_claim = dict(projected.alias_to_claim or {})
    return request


class DraftSupport(Contract):
    claim_id: Key
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class DraftUnit(Contract):
    temp_key: Key
    text: ShortText
    claim_refs: tuple[Key, ...] = Field(default=(), max_length=30)
    support: tuple[DraftSupport, ...] = Field(default=(), max_length=30)
    purpose: Literal["experience", "proposal", "summary", "skill"]
    plan_ref: Key | None = None

    @model_validator(mode="after")
    def check_support(self):
        if len(set(self.claim_refs)) != len(self.claim_refs):
            raise ValueError("duplicate claim reference")
        for item in self.support:
            if item.claim_id not in self.claim_refs or item.end <= item.start or item.end > len(self.text):
                raise ValueError("invalid support range")
        if self.purpose == "proposal" and self.plan_ref is None:
            raise ValueError("proposal requires a plan")
        return self


class DraftEntry(Contract):
    temp_key: Key
    entity_ref: Key | None = None
    organization_ref: Key | None = None
    role_ref: Key | None = None
    date_ref: Key | None = None
    school_ref: Key | None = None
    major_ref: Key | None = None
    degree_ref: Key | None = None
    name: Annotated[str, Field(max_length=200)] | None = None
    category: Annotated[str, Field(max_length=200)] | None = None
    display_aliases: dict[Literal["organization_ref", "role_ref", "school_ref", "major_ref", "degree_ref", "date_ref"], DisplayAlias] = Field(default_factory=dict, exclude_if=lambda value: not value)
    plan_ref: Key | None = None
    units: tuple[DraftUnit, ...] = Field(default=(), max_length=40)

    @model_validator(mode="after")
    def check_display_aliases(self):
        validate_entry_display_aliases(self)
        if any(_contains_contact(alias.text, ()) and not (field == "date_ref" and is_explicit_year_range(alias.text))
               for field, alias in self.display_aliases.items()):
            raise ValueError("display alias requires privacy review")
        return self


class DraftSection(Contract):
    temp_key: Key
    type: Literal["experience", "projects", "skills", "education"]
    entries: tuple[DraftEntry, ...] = Field(max_length=40)

    @model_validator(mode="after")
    def project_plan(self):
        if self.type == "projects" and any(entry.plan_ref is None for entry in self.entries):
            raise ValueError("project requires plan")
        return self


class DraftDocument(Contract):
    kind: Kind
    locale: Locale
    title: Annotated[str, Field(min_length=1, max_length=160, pattern=r"\S")]
    summary: DraftUnit
    sections: tuple[DraftSection, ...] = Field(max_length=30)

    @model_validator(mode="after")
    def check_keys(self):
        if self.summary.purpose != "summary":
            raise ValueError("summary purpose required")
        keys = [self.summary.temp_key]
        for section in self.sections:
            keys.append(section.temp_key)
            for entry in section.entries:
                keys.append(entry.temp_key)
                keys.extend(unit.temp_key for unit in entry.units)
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate temporary key")
        return self


class DraftPlan(Contract):
    temp_key: Key
    title: ShortText
    background: ShortText
    task: ShortText
    actions: tuple[ShortText, ...] = Field(min_length=1, max_length=20)
    outcome: ShortText
    verification: ShortText
    resource_constraints: tuple[ShortText, ...] = Field(default=(), max_length=20)


class BundleCandidate(Contract):
    job: DraftDocument
    blueprint: DraftDocument
    plans: tuple[DraftPlan, ...] = Field(max_length=40)


def _bound_output(payload: object) -> None:
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False)) > 200_000:
        raise ValueError("provider output exceeds limit")


def validate_bundle_output(payload: object, request: WriterRequest) -> BundleCandidate:
    """Validate temporary keys/references; application assigns IDs once later."""
    _bound_output(payload)
    result = BundleCandidate.model_validate(payload)
    if result.job.kind != "job" or result.blueprint.kind != "blueprint":
        raise ValueError("document kind mismatch")
    if any(doc.locale != request.resume_locale for doc in (result.job, result.blueprint)):
        raise ValueError("document locale mismatch")
    if not request.facts:
        required_summary = "Add your background and career goal." if request.resume_locale == "en" else "请填写个人背景与求职目标。"
        if result.job.summary.text != required_summary or result.job.summary.claim_refs:
            raise ValueError("empty facts require the fixed job summary skeleton")
        if any(entry.units or (entry.name and entry.name.strip()) or (entry.category and entry.category.strip())
               or any((entry.entity_ref, entry.organization_ref, entry.role_ref, entry.date_ref,
                       entry.school_ref, entry.major_ref, entry.degree_ref))
               for section in result.job.sections for entry in section.entries):
            raise ValueError("empty facts cannot create job experience units or visible claims")
    # Preserve at least one visible provided background fact in each relevant
    # category; never force a choice when all candidates are disputed.
    eligible_work = {fact.claim_id for fact in request.facts if fact.fact_type in {"employer", "job_title"} and fact.value and fact.confirmation != "disputed"}
    eligible_education = {fact.claim_id for fact in request.facts if fact.fact_type in {"school", "major", "degree"} and fact.value and fact.confirmation != "disputed"}
    work_refs = {ref for section in result.blueprint.sections if section.type == "experience" for entry in section.entries for ref in (entry.organization_ref, entry.role_ref) if ref}
    education_refs = {ref for section in result.blueprint.sections if section.type == "education" for entry in section.entries for ref in (entry.school_ref, entry.major_ref, entry.degree_ref) if ref}
    if eligible_work and not eligible_work.intersection(work_refs):
        raise ValueError("blueprint omits provided work background")
    if eligible_education and not eligible_education.intersection(education_refs):
        raise ValueError("blueprint omits provided education background")
    claim_types = {fact.claim_id: fact.fact_type for fact in request.facts}
    claim_ids = set(claim_types)
    plan_ids = [plan.temp_key for plan in result.plans]
    if len(plan_ids) != len(set(plan_ids)):
        raise ValueError("duplicate plan key")
    for document in (result.job, result.blueprint):
        units = [document.summary]
        for section in document.sections:
            for entry in section.entries:
                refs = (entry.entity_ref, entry.organization_ref, entry.role_ref, entry.date_ref,
                        entry.school_ref, entry.major_ref, entry.degree_ref)
                if any(ref is not None and ref not in claim_ids for ref in refs):
                    raise ValueError("unknown entry claim")
                validate_entry_claim_types(entry, section.type, claim_types)
                if any(alias.locale != document.locale for alias in entry.display_aliases.values()):
                    raise ValueError("display alias locale differs from document")
                validate_display_alias_values(entry, {fact.claim_id: fact.value for fact in request.facts})
                if entry.plan_ref is not None and entry.plan_ref not in plan_ids:
                    raise ValueError("unknown plan")
                if entry.plan_ref is not None and any(unit.plan_ref is not None and unit.plan_ref != entry.plan_ref for unit in entry.units):
                    raise ValueError("unit plan does not match entry plan")
                if section.type != "projects" and entry.name is not None:
                    raise ValueError("uncited entry name")
                units.extend(entry.units)
        for unit in units:
            if not set(unit.claim_refs) <= claim_ids:
                raise ValueError("unknown unit claim")
            if unit.plan_ref is not None and unit.plan_ref not in plan_ids:
                raise ValueError("unknown plan")
    # Provider references are temporary aliases. Only after validating every
    # reference do we map them back to the frozen, application-owned IDs.
    if request._alias_to_claim:
        raw = result.model_dump(mode="json")
        for document in (raw["job"], raw["blueprint"]):
            units = [document["summary"]]
            for section in document["sections"]:
                for entry in section["entries"]:
                    for key in ("entity_ref", "organization_ref", "role_ref", "date_ref", "school_ref", "major_ref", "degree_ref"):
                        if entry[key] is not None:
                            entry[key] = request._alias_to_claim[entry[key]]
                    units.extend(entry["units"])
            for unit in units:
                unit["claim_refs"] = [request._alias_to_claim[ref] for ref in unit["claim_refs"]]
                for span in unit["support"]:
                    span["claim_id"] = request._alias_to_claim[span["claim_id"]]
        result = BundleCandidate.model_validate(raw)
    return result


class GuideUnit(Contract):
    unit_id: Key
    text: ShortText
    completion: Literal["completed", "in_progress", "planned", "unknown", "not_applicable"]
    authorship: Literal["user_text", "ai_rewrite", "ai_proposed", "user_edited"] = "ai_proposed"
    purpose: Literal["experience", "proposal", "summary", "skill"] = "experience"
    claim_refs: tuple[Key, ...] = Field(default=(), max_length=30)
    plan_ref: Key | None = None


class GuideFact(WriterFact):
    source_types: tuple[Literal["text", "docx", "pdf", "manual"], ...] = ()
    has_evidence: bool = False


PlanField = Literal["title", "background", "task", "actions", "outcome", "verification", "resource_constraints"]


class GuidePlan(Contract):
    key: Key
    title: ShortText
    background: ShortText
    task: ShortText
    actions: tuple[ShortText, ...] = Field(default=(), max_length=300)
    outcome: ShortText
    verification: ShortText
    resource_constraints: tuple[ShortText, ...] = Field(default=(), max_length=100)
    needs_review: tuple[PlanField, ...] = Field(default=(), max_length=100)
    field_authorship: dict[PlanField, Literal["user_edited_unconfirmed", "derived_from_saved_units"]] = Field(default_factory=dict)


class GuideRequest(Contract):
    resume_id: Key
    resume_revision: int = Field(ge=1)
    semantic_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    kind: Kind
    guide_locale: Locale
    jd_text: Annotated[str, Field(min_length=1, max_length=15000)]
    requirement_ids: tuple[Key, ...] = Field(default=(), max_length=100)
    units: tuple[GuideUnit, ...] = Field(max_length=300)
    facts: tuple[GuideFact, ...] = Field(default=(), max_length=200)
    plans: tuple[GuidePlan, ...] = Field(default=(), max_length=40)

    @model_validator(mode="after")
    def unique_ids(self):
        if _contains_contact(self.jd_text, ()):
            raise ValueError("JD requires privacy review")
        if any(_contains_contact(unit.text, ()) or contains_sensitive_identifier(unit.unit_id, ()) for unit in self.units) or any(contains_sensitive_identifier(ref, ()) for ref in self.requirement_ids):
            raise ValueError("guide input requires privacy review")
        ids = [unit.unit_id for unit in self.units]
        if len(ids) != len(set(ids)) or len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError("duplicate guide input id")
        facts = {fact.claim_id for fact in self.facts}
        plans = {plan.key for plan in self.plans}
        if len(facts) != len(self.facts) or len(plans) != len(self.plans):
            raise ValueError("duplicate guide fact or plan")
        if any(not set(fact.conflict_ids) <= facts for fact in self.facts):
            raise ValueError("unknown guide fact conflict")
        if any(not set(unit.claim_refs) <= facts or (unit.plan_ref and unit.plan_ref not in plans) for unit in self.units):
            raise ValueError("unknown guide unit reference")
        return self


def build_guide_request(*, private_header: dict, additional_private_values: tuple[str, ...] = (), **payload) -> GuideRequest:
    """Reject identified information in frozen inputs.

    Before wiring this to an endpoint, the service must compute the semantic
    hash itself from the saved revision and bind it atomically. Validating its
    format here does not establish its origin or authorize client-supplied hashes.
    """
    private_values = tuple(value.strip() for value in (*private_header.values(), *additional_private_values)
                           if isinstance(value, str) and value.strip())
    # resume_id, revision and hash are local binding metadata. The compatible
    # provider explicitly excludes them; strict types still validate their form.
    request = GuideRequest.model_validate(payload)
    if (_contains_contact(request.jd_text, private_values)
        or any(contains_sensitive_identifier(ref, private_values) for ref in request.requirement_ids)
        or any(_contains_contact(unit.text, private_values) or contains_sensitive_identifier(unit.unit_id, private_values)
               or any(contains_sensitive_identifier(ref, private_values) for ref in unit.claim_refs)
               or (unit.plan_ref and contains_sensitive_identifier(unit.plan_ref, private_values))
               for unit in request.units)
        or any((fact.value and _contains_contact(fact.value, private_values))
               or contains_sensitive_identifier(fact.claim_id, private_values)
               or any(contains_sensitive_identifier(ref, private_values) for ref in fact.conflict_ids)
               for fact in request.facts)
        or any(contains_sensitive_identifier(plan.key, private_values)
               or any(_contains_contact(value, private_values) for value in
                      (plan.title, plan.background, plan.task, *plan.actions, plan.outcome, plan.verification, *plan.resource_constraints))
               for plan in request.plans)):
        raise ValueError("guide input requires privacy review")
    return request


class GuideEntry(Contract):
    unit_ids: tuple[Key, ...] = Field(min_length=1, max_length=30)
    question: ShortText
    answer_outline: ShortText


class ContextNote(Contract):
    text: ShortText
    requirement_ids: tuple[Key, ...] = Field(default=(), max_length=30)


class GuideCandidate(Contract):
    kind: Kind
    locale: Locale
    entries: tuple[GuideEntry, ...] = Field(max_length=300)
    context_notes: tuple[ContextNote, ...] = Field(default=(), max_length=100)


# Only reject implementation metadata in user-visible prose. A role's genuine
# AI/ML skills, and ordinary language about checking facts, remain allowed.
_INTERNAL_GUIDE_NAMES = re.compile(
    r"(?<![A-Za-z0-9_])(?:claim_refs|claim_id|unit_ids|unit_id|plan_ref|source_types|"
    r"field_authorship|needs_review|"
    r"user_confirmed|unconfirmed|ai_rewrite|ai_proposed|user_edited_unconfirmed|"
    r"derived_from_saved_units|not_applicable|in_progress)(?![A-Za-z0-9_])", re.I,
)
_INTERNAL_GUIDE_PROSE = re.compile(
    r"(?:单位|单元|条目|字段|系统|模型)(?:文本|内容|字段)?\s*"
    r"(?:标为|标记为|标注为|状态为|属于|是)\s*(?:AI|人工智能|系统|机器|模型)?\s*"
    r"(?:改写|生成|提出|编辑|标注|待审)"
    r"|(?:来源|证据)(?:标记|字段|类型)\s*(?:为|是|显示|表明|说明)"
    r"|(?<![A-Za-z0-9_])(?:confirmation|completion|authorship)\s*[:=]"
    r"|(?:field|attribute|metadata)\s+(?:confirmation|completion|authorship)(?![A-Za-z0-9_])", re.I,
)


def _validate_guide_visible_prose(result: GuideCandidate) -> None:
    texts = [value for entry in result.entries for value in (entry.question, entry.answer_outline)]
    texts.extend(note.text for note in result.context_notes)
    if any(_INTERNAL_GUIDE_NAMES.search(value) or _INTERNAL_GUIDE_PROSE.search(value) for value in texts):
        raise ValueError("guide contains internal metadata terms")


def validate_guide_output(payload: object, request: GuideRequest) -> GuideCandidate:
    _bound_output(payload)
    result = GuideCandidate.model_validate(payload)
    if result.kind != request.kind or result.locale != request.guide_locale:
        raise ValueError("guide kind or locale mismatch")
    _validate_guide_visible_prose(result)
    if any(not set(note.requirement_ids) <= set(request.requirement_ids) for note in result.context_notes):
        raise ValueError("unknown requirement reference")
    validate_guide_coverage(
        [{"unit_ids": list(entry.unit_ids), "question": entry.question, "answer_outline": entry.answer_outline} for entry in result.entries],
        [note.model_dump() for note in result.context_notes], [unit.unit_id for unit in request.units],
    )
    return result


class WriterProviderV2(Protocol):
    """Provider adapters accept only pre-projected requests and return candidates."""
    def bundle(self, request: WriterRequest) -> object: ...
    def guide(self, request: GuideRequest) -> object: ...
