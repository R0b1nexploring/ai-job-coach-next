"""Versioned, offline contracts for the S1 content model.

These types do not persist data or establish that a statement is true.  Evidence
coordinates refer only to the exact, newline-normalized material revision named
by the evidence; callers must validate them against the stored revisions.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]
NonEmpty = Annotated[str, Field(min_length=1)]
Confirmation = Literal["unconfirmed", "user_confirmed", "disputed", "unknown"]
Completion = Literal["completed", "in_progress", "planned", "unknown", "not_applicable"]
Authorship = Literal["user_text", "ai_rewrite", "ai_proposed", "user_edited"]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_material_text(text: str) -> str:
    """Normalize before storage and before computing Unicode codepoint offsets."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MaterialRevision(Contract):
    revision_id: Identifier
    private_scope: Identifier
    source_type: Literal["text", "docx", "pdf", "manual"]
    extracted_text: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def check_content(self):
        if "\r" in self.extracted_text:
            raise ValueError("material text must use normalized newlines")
        if self.content_hash != text_hash(self.extracted_text):
            raise ValueError("material hash does not match extracted text")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return self


class ClaimEvidence(Contract):
    source_revision_id: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    page: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def check_range(self):
        if self.end <= self.start:
            raise ValueError("evidence must select a non-empty range")
        return self


class Claim(Contract):
    claim_id: Identifier
    fact_type: NonEmpty
    value: str | None
    evidence: tuple[ClaimEvidence, ...] = ()
    confirmation: Confirmation = "unconfirmed"
    completion: Completion = "unknown"
    conflict_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def check_conflicts(self):
        if self.claim_id in self.conflict_ids or len(set(self.conflict_ids)) != len(self.conflict_ids):
            raise ValueError("invalid conflict IDs")
        return self


class FactSnapshot(Contract):
    snapshot_id: Identifier
    private_scope: Identifier
    claims: tuple[Claim, ...]

    @model_validator(mode="after")
    def check_claims(self):
        ids = [claim.claim_id for claim in self.claims]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate claim ID")
        known = set(ids)
        if any(ref not in known for claim in self.claims for ref in claim.conflict_ids):
            raise ValueError("unknown conflicting claim")
        return self


def validate_snapshot_evidence(snapshot: FactSnapshot, revisions: tuple[MaterialRevision, ...]) -> None:
    """Reject references outside scope or ranges/hashes that do not match source."""
    by_id = {revision.revision_id: revision for revision in revisions}
    if len(by_id) != len(revisions):
        raise ValueError("duplicate material revision ID")
    for claim in snapshot.claims:
        for evidence in claim.evidence:
            revision = by_id.get(evidence.source_revision_id)
            if revision is None or revision.private_scope != snapshot.private_scope:
                raise ValueError("unknown or out-of-scope material revision")
            if evidence.end > len(revision.extracted_text):
                raise ValueError("evidence exceeds source length")
            excerpt = revision.extracted_text[evidence.start:evidence.end]
            if text_hash(excerpt) != evidence.quote_hash:
                raise ValueError("evidence hash does not match source range")


class SupportRange(Contract):
    """Portion of a unit linked to the specified claim, not its whole sentence."""
    claim_id: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def check_range(self):
        if self.end <= self.start:
            raise ValueError("support must select a non-empty range")
        return self


class NarrativeUnit(Contract):
    unit_id: Identifier
    text: NonEmpty
    claim_refs: tuple[Identifier, ...] = ()
    support: tuple[SupportRange, ...] = ()
    authorship: Authorship
    completion: Completion
    purpose: Literal["experience", "proposal", "summary", "skill"]
    plan_ref: Identifier | None = None
    locked: bool = False

    @model_validator(mode="after")
    def check_support(self):
        if not self.text.strip():
            raise ValueError("unit text cannot be blank")
        if len(set(self.claim_refs)) != len(self.claim_refs):
            raise ValueError("duplicate claim reference")
        for span in self.support:
            if span.claim_id not in self.claim_refs or span.end > len(self.text):
                raise ValueError("support range or claim reference is invalid")
        return self


class DisplayAlias(Contract):
    locale: Literal["zh-CN", "en"]
    text: Annotated[str, Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def nonblank(self):
        if not self.text.strip():
            raise ValueError("display alias cannot be blank")
        return self


class ResumeEntry(Contract):
    entry_id: Identifier
    # Values are claim references, never an untracked copy of a factual value.
    entity_ref: Identifier | None = None
    organization_ref: Identifier | None = None
    role_ref: Identifier | None = None
    date_ref: Identifier | None = None
    school_ref: Identifier | None = None
    major_ref: Identifier | None = None
    degree_ref: Identifier | None = None
    name: str | None = None
    category: str | None = None
    # Language-specific display only; each key must name an existing ref. The
    # referenced source fact remains authoritative and is never overwritten.
    display_aliases: dict[Literal["organization_ref", "role_ref", "school_ref", "major_ref", "degree_ref", "date_ref"], DisplayAlias] = Field(default_factory=dict, exclude_if=lambda value: not value)
    plan_ref: Identifier | None = None
    units: tuple[NarrativeUnit, ...] = ()

    @model_validator(mode="after")
    def check_display_aliases(self):
        validate_entry_display_aliases(self)
        return self


def validate_entry_display_aliases(entry) -> None:
    for field, alias in entry.display_aliases.items():
        if not getattr(entry, field, None) or not alias.text.strip():
            raise ValueError("display alias requires a nonempty corresponding fact reference")


class ResumeSection(Contract):
    section_id: Identifier
    type: Literal["experience", "projects", "skills", "education"]
    entries: tuple[ResumeEntry, ...]

    @model_validator(mode="after")
    def check_entries(self):
        for entry in self.entries:
            if self.type == "projects" and entry.plan_ref is None:
                raise ValueError("project entries require a plan snapshot reference")
        return self


class ResumeDocumentV2(Contract):
    schema_version: Literal[2] = 2
    kind: Literal["job", "blueprint"]
    locale: Literal["zh-CN", "en"]
    title: NonEmpty
    summary: NarrativeUnit
    sections: tuple[ResumeSection, ...]

    @model_validator(mode="after")
    def check_ids_and_summary(self):
        if not self.title.strip() or self.summary.purpose != "summary":
            raise ValueError("title and summary must be valid")
        sections = [section.section_id for section in self.sections]
        entries = [entry.entry_id for section in self.sections for entry in section.entries]
        units = [self.summary.unit_id] + [unit.unit_id for section in self.sections for entry in section.entries for unit in entry.units]
        if any(len(ids) != len(set(ids)) for ids in (sections, entries, units)):
            raise ValueError("duplicate section, entry or unit ID")
        return self


def validate_entry_claim_types(entry, section_type: str, claim_types: dict[str, str]) -> None:
    """A reference's existence does not establish it is the right fact type."""
    expected = {"organization_ref": "employer", "role_ref": "job_title", "school_ref": "school",
                "major_ref": "major", "degree_ref": "degree"}
    if entry.date_ref is not None:
        if section_type not in {"experience", "education"}:
            raise ValueError("date reference requires experience or education")
        expected["date_ref"] = "employment_date" if section_type == "experience" else "education_date"
    for field, fact_type in expected.items():
        ref = getattr(entry, field)
        if ref is not None and claim_types.get(ref) != fact_type:
            raise ValueError(f"{field} references an incompatible fact type")


def is_explicit_year_range(value: str, private_values: tuple[str, ...] = ()) -> bool:
    """Only a complete ascending four-digit year range can bypass phone detection.

    This is a shape check, not proof of identity or factual correctness. Callers
    must separately validate the bound fact type and source years.
    """
    if not isinstance(value, str) or any(token and token.casefold() in value.casefold() for token in private_values):
        return False
    match = re.fullmatch(r"\s*((?:19|20)[0-9]{2})\s*[-–—]\s*((?:19|20)[0-9]{2})\s*", value)
    return bool(match and int(match.group(1)) <= int(match.group(2)))


def validate_display_alias_values(entry, values_by_id: dict[str, str | None]) -> None:
    for field, alias in entry.display_aliases.items():
        original = values_by_id.get(getattr(entry, field))
        if not isinstance(original, str) or not original.strip():
            raise ValueError("display alias cannot replace an unknown fact")
        if field == "date_ref":
            # A translated date must retain the supplied four-digit years. This
            # catches altered/invented years, not every possible semantic error.
            years = lambda text: re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", text)
            if years(original) != years(alias.text) or (not years(original) and alias.text != original):
                raise ValueError("date alias changes or invents a year")
            # A bare year range is not an adequate translation of a more
            # precise source; it would erase known months or days.
            month_or_day = re.search(r"年\s*[0-9]{1,2}\s*月|(?:19|20)[0-9]{2}[-/.][0-9]{1,2}(?![0-9])", original)
            if month_or_day and is_explicit_year_range(alias.text):
                raise ValueError("date alias drops source month precision")


def validate_document_references(document: ResumeDocumentV2, snapshot: FactSnapshot, plan_refs: set[str]) -> None:
    """Check referential integrity against a frozen snapshot and plan set.

    This checks only references, not whether their evidence semantically supports
    the wording.  Such decisions require the separate review pipeline.
    """
    claim_types = {claim.claim_id: claim.fact_type for claim in snapshot.claims}
    claim_ids = set(claim_types)
    units = [document.summary]
    for section in document.sections:
        for entry in section.entries:
            refs = (entry.entity_ref, entry.organization_ref, entry.role_ref, entry.date_ref,
                    entry.school_ref, entry.major_ref, entry.degree_ref)
            if any(ref is not None and ref not in claim_ids for ref in refs):
                raise ValueError("entry references an unknown claim")
            validate_entry_claim_types(entry, section.type, claim_types)
            validate_display_alias_values(entry, {claim.claim_id: claim.value for claim in snapshot.claims})
            if entry.plan_ref is not None and entry.plan_ref not in plan_refs:
                raise ValueError("entry references an unknown plan")
            units.extend(entry.units)
    for unit in units:
        if not set(unit.claim_refs) <= claim_ids:
            raise ValueError("unit references an unknown claim")
        if unit.plan_ref is not None and unit.plan_ref not in plan_refs:
            raise ValueError("unit references an unknown plan")
        if unit.purpose == "proposal" and unit.plan_ref is None:
            raise ValueError("proposal requires a plan snapshot reference")


class LegacyProjectProjection(Contract):
    """Read-only v1 view; intentionally not a ResumeDocumentV2 revision."""
    item_id: str | None = None
    plan_key: str | None = None
    text: str
    source_status: str | None = None
    verification_status: str | None = None
    completion: Literal["completed", "unknown"]
    legacy_achieved: bool | None = None


class LegacyResumeProjection(Contract):
    title: str
    summary: str
    projects: tuple[LegacyProjectProjection, ...]


def project_v1_read_only(resume: dict) -> LegacyResumeProjection:
    """Project legacy items for display without mutating or upgrading history."""
    if "sections" in resume or resume.get("schema_version", 1) != 1:
        raise ValueError("expected a v1 resume")
    projects = []
    for item in resume.get("items", []):
        achieved = item.get("achieved")
        if achieved is not None and type(achieved) is not bool:
            raise ValueError("legacy achieved must be boolean or missing")
        projects.append(LegacyProjectProjection(
            item_id=item.get("id"), plan_key=item.get("plan_key"), text=item["text"],
            source_status=item.get("source_status"), verification_status=item.get("verification_status"),
            completion="completed" if achieved is True else "unknown", legacy_achieved=achieved,
        ))
    return LegacyResumeProjection(title=resume.get("title", ""), summary=resume.get("summary", ""), projects=tuple(projects))
