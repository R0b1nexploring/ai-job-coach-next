from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.content_v2 import (
    Claim,
    ClaimEvidence,
    FactSnapshot,
    MaterialRevision,
    NarrativeUnit,
    ResumeDocumentV2,
    ResumeEntry,
    ResumeSection,
    normalize_material_text,
    project_v1_read_only,
    text_hash,
    validate_document_references,
    validate_snapshot_evidence,
)


def sample():
    source = normalize_material_text("虚构公司\r\n参与整理设备记录🙂。")
    revision = MaterialRevision(
        revision_id="source-1", private_scope="local", source_type="manual",
        extracted_text=source, content_hash=text_hash(source),
        created_at=datetime(2026, 10, 31, tzinfo=timezone.utc),
    )
    quote = "整理设备记录🙂"
    start = source.index(quote)
    evidence = ClaimEvidence(source_revision_id="source-1", start=start, end=start + len(quote), quote_hash=text_hash(quote))
    claim = Claim(claim_id="claim-1", fact_type="activity", value=quote, evidence=(evidence,))
    snapshot = FactSnapshot(snapshot_id="facts-1", private_scope="local", claims=(claim,))
    summary = NarrativeUnit(unit_id="summary-1", text="参与设备记录整理", claim_refs=("claim-1",),
                            authorship="ai_rewrite", completion="unknown", purpose="summary")
    unit = NarrativeUnit(unit_id="unit-1", text="整理设备记录并独立主导项目", claim_refs=("claim-1",),
                         support=({"claim_id": "claim-1", "start": 0, "end": 6},),
                         authorship="ai_rewrite", completion="unknown", purpose="experience")
    entry = ResumeEntry(entry_id="entry-1", units=(unit,))
    section = ResumeSection(section_id="section-1", type="experience", entries=(entry,))
    document = ResumeDocumentV2(kind="job", locale="zh-CN", title="工程师", summary=summary, sections=(section,))
    return revision, claim, snapshot, document


def test_snapshot_validates_offsets_against_exact_revision_and_scope():
    revision, claim, snapshot, _ = sample()
    validate_snapshot_evidence(snapshot, (revision,))
    other = revision.model_copy(update={"revision_id": "source-2"})
    with pytest.raises(ValueError, match="unknown or out-of-scope"):
        validate_snapshot_evidence(snapshot, (other,))
    with pytest.raises(ValueError, match="out-of-scope"):
        validate_snapshot_evidence(snapshot.model_copy(update={"private_scope": "other"}), (revision,))
    old_evidence = claim.evidence[0]
    wrong_evidence = ClaimEvidence(source_revision_id="source-1", start=old_evidence.start + 1,
                                   end=old_evidence.end, quote_hash=old_evidence.quote_hash)
    bad = FactSnapshot(snapshot_id="facts-2", private_scope="local", claims=(claim.model_copy(update={"evidence": (wrong_evidence,)}),))
    with pytest.raises(ValueError, match="hash"):
        validate_snapshot_evidence(bad, (revision,))


def test_material_requires_normalized_text_matching_hash_and_aware_time():
    with pytest.raises(ValidationError, match="normalized"):
        MaterialRevision(revision_id="s", private_scope="local", source_type="text", extracted_text="a\r\nb",
                         content_hash=text_hash("a\r\nb"), created_at=datetime.now(timezone.utc))
    with pytest.raises(ValidationError, match="hash"):
        MaterialRevision(revision_id="s", private_scope="local", source_type="text", extracted_text="b",
                         content_hash=text_hash("a"), created_at=datetime.now(timezone.utc))
    with pytest.raises(ValidationError, match="timezone"):
        MaterialRevision(revision_id="s", private_scope="local", source_type="text", extracted_text="a",
                         content_hash=text_hash("a"), created_at=datetime(2026, 1, 1))


def test_snapshot_contract_preserves_independent_states_and_conflicts():
    first = Claim(claim_id="c1", fact_type="role", value="工程师", confirmation="disputed", completion="unknown", conflict_ids=("c2",))
    second = Claim(claim_id="c2", fact_type="role", value="助理", confirmation="unconfirmed", completion="unknown", conflict_ids=("c1",))
    snap = FactSnapshot(snapshot_id="s", private_scope="local", claims=(first, second))
    assert snap.claims[0].confirmation == "disputed"
    with pytest.raises(ValidationError, match="unknown conflicting"):
        FactSnapshot(snapshot_id="s", private_scope="local", claims=(first,))
    with pytest.raises(ValidationError, match="duplicate claim"):
        FactSnapshot(snapshot_id="s", private_scope="local", claims=(first, first))
    with pytest.raises(ValidationError):
        first.confirmation = "user_confirmed"


def test_document_reference_integrity_and_partial_support():
    _, _, snapshot, document = sample()
    validate_document_references(document, snapshot, set())
    unit = document.sections[0].entries[0].units[0]
    assert unit.support[0].end < len(unit.text)  # A cited part does not ground the extra claim.
    bad = document.model_dump()
    bad["sections"][0]["entries"][0]["role_ref"] = "not-in-snapshot"
    with pytest.raises(ValueError, match="unknown claim"):
        validate_document_references(ResumeDocumentV2.model_validate(bad), snapshot, set())
    bad = document.model_dump()
    bad["sections"][0]["entries"][0]["units"][0]["claim_refs"] = ["unknown"]
    bad["sections"][0]["entries"][0]["units"][0]["support"] = []
    with pytest.raises(ValueError, match="unknown claim"):
        validate_document_references(ResumeDocumentV2.model_validate(bad), snapshot, set())


def test_document_rejects_legacy_items_duplicate_ids_and_invalid_support():
    _, _, _, doc = sample()
    raw = doc.model_dump()
    raw["items"] = []
    with pytest.raises(ValidationError):
        ResumeDocumentV2.model_validate(raw)
    raw = doc.model_dump()
    raw["sections"][0]["entries"][0]["units"][0]["unit_id"] = raw["summary"]["unit_id"]
    with pytest.raises(ValidationError, match="duplicate"):
        ResumeDocumentV2.model_validate(raw)
    raw = doc.model_dump()
    raw["sections"][0]["entries"][0]["units"][0]["support"][0]["end"] = 999
    with pytest.raises(ValidationError, match="support"):
        ResumeDocumentV2.model_validate(raw)


def test_project_requires_existing_plan_and_proposal_requires_plan():
    _, _, snapshot, document = sample()
    raw = document.model_dump()
    raw["sections"][0]["type"] = "projects"
    with pytest.raises(ValidationError, match="plan"):
        ResumeDocumentV2.model_validate(raw)
    raw["sections"][0]["entries"][0]["plan_ref"] = "plan-1"
    raw["sections"][0]["entries"][0]["units"][0]["purpose"] = "proposal"
    raw["sections"][0]["entries"][0]["units"][0]["plan_ref"] = "plan-1"
    project = ResumeDocumentV2.model_validate(raw)
    with pytest.raises(ValueError, match="unknown plan"):
        validate_document_references(project, snapshot, set())
    validate_document_references(project, snapshot, {"plan-1"})


def test_legacy_projection_preserves_old_status_and_does_not_invent_facts():
    old = {"title": "旧版", "summary": "简介", "items": [{"id": "a", "text": "项目", "achieved": False, "source_status": "user_confirmed"}]}
    before = repr(old)
    view = project_v1_read_only(old)
    assert view.projects[0].completion == "unknown"
    assert view.projects[0].legacy_achieved is False
    assert view.projects[0].source_status == "user_confirmed"
    assert repr(old) == before
    assert not hasattr(view.projects[0], "organization_ref")
    with pytest.raises(ValueError, match="v1"):
        project_v1_read_only({**old, "sections": []})


@pytest.mark.parametrize(("section_type", "field", "fact_type"), [
    ("experience", "organization_ref", "employer"),
    ("experience", "role_ref", "job_title"),
    ("experience", "date_ref", "employment_date"),
    ("education", "school_ref", "school"),
    ("education", "major_ref", "major"),
    ("education", "degree_ref", "degree"),
    ("education", "date_ref", "education_date"),
])
def test_factual_display_references_require_correct_type(section_type, field, fact_type):
    _, _, _, doc = sample()
    raw = doc.model_dump()
    raw["sections"][0]["type"] = section_type
    raw["sections"][0]["entries"][0][field] = "display"
    record = Claim(claim_id="display", fact_type=fact_type, value="虚构事实")
    action = Claim(claim_id="claim-1", fact_type="action", value="事实")
    validate_document_references(ResumeDocumentV2.model_validate(raw),
        FactSnapshot(snapshot_id="s", private_scope="local", claims=(record, action)), set())
    wrong = record.model_copy(update={"fact_type": "action"})
    with pytest.raises(ValueError, match="incompatible fact type"):
        validate_document_references(ResumeDocumentV2.model_validate(raw),
            FactSnapshot(snapshot_id="s", private_scope="local", claims=(wrong, action)), set())
