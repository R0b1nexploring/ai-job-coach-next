from copy import deepcopy
from io import BytesIO

import pytest
from docx import Document
from pydantic import ValidationError

from app.content_v2 import Claim, FactSnapshot, ResumeDocumentV2, validate_document_references
from app.files_v2 import render_docx, render_html, resume_blocks
from app.generation_v2 import LocalWriterV2, generate_bundle_v2
from app.guide_v2 import semantic_hash
from app.provider_contract_v2 import validate_bundle_output, WriterRequest


def fixture():
    facts = FactSnapshot(snapshot_id="s", private_scope="local", claims=(
        Claim(claim_id="role", fact_type="job_title", value="设备工程师"),
        Claim(claim_id="major", fact_type="major", value="机械工程"),
        Claim(claim_id="work", fact_type="action", value="整理设备记录"),
    ))
    def unit(id, text, purpose):
        return {"unit_id": id, "text": text, "authorship": "ai_rewrite", "completion": "unknown", "purpose": purpose}
    doc = {"kind":"job", "locale":"en", "title":"Engineer", "summary":unit("s1", "Engineering profile", "summary"),
           "sections":[{"section_id":"exp", "type":"experience", "entries":[{"entry_id":"e1", "role_ref":"role", "display_aliases":{"role_ref":{"locale":"en", "text":"Equipment Engineer"}}, "units":[unit("u1", "Organized equipment records", "experience")]}]},
                       {"section_id":"edu", "type":"education", "entries":[{"entry_id":"e2", "major_ref":"major", "display_aliases":{"major_ref":{"locale":"en", "text":"Mechanical Engineering"}}, "units":[]}]}]}
    return facts, doc


def test_english_aliases_render_from_bound_chinese_fact_without_changing_source():
    facts, raw = fixture(); original = deepcopy(facts.model_dump())
    doc = ResumeDocumentV2.model_validate(raw)
    validate_document_references(doc, facts, set())
    blocks = resume_blocks(doc, facts, {}, draft=True)
    html = render_html(blocks, "en").decode()
    text = "\n".join(p.text for p in Document(BytesIO(render_docx(blocks))).paragraphs)
    assert "Equipment Engineer" in html and "Mechanical Engineering" in text
    assert "设备工程师" not in html and facts.model_dump() == original
    assert facts.claims[0].confirmation == "unconfirmed"
    different = deepcopy(raw); different["sections"][0]["entries"][0]["display_aliases"]["role_ref"]["text"] = "Alternate"
    assert semantic_hash(raw, {}, {}, []) != semantic_hash(different, {}, {}, [])


def test_alias_falls_back_to_source_for_other_document_locale():
    facts, raw = fixture();raw["locale"] = "zh-CN"
    text = " ".join(value for _, value in resume_blocks(ResumeDocumentV2.model_validate(raw), facts, {}, draft=True))
    assert "设备工程师" in text and "Equipment Engineer" not in text


def test_rejects_alias_without_ref_unknown_field_unknown_fact_and_wrong_type():
    facts, raw = fixture()
    del raw["sections"][0]["entries"][0]["role_ref"]
    with pytest.raises(ValidationError): ResumeDocumentV2.model_validate(raw)
    facts, raw = fixture(); raw["sections"][0]["entries"][0]["display_aliases"]={"text":{"locale":"en","text":"Forged"}}
    with pytest.raises(ValidationError): ResumeDocumentV2.model_validate(raw)
    facts, raw = fixture(); raw["sections"][0]["entries"][0]["role_ref"] = "work"
    doc=ResumeDocumentV2.model_validate(raw)
    with pytest.raises(ValueError,match="incompatible fact type"):validate_document_references(doc,facts,set())
    with pytest.raises(ValueError,match="incompatible fact type"):resume_blocks(doc,facts,{},draft=True)
    facts, raw = fixture(); raw["sections"][0]["entries"][0]["role_ref"] = "missing"
    with pytest.raises(ValueError,match="unknown claim"):validate_document_references(ResumeDocumentV2.model_validate(raw),facts,set())
    facts, raw = fixture(); null_facts=FactSnapshot(snapshot_id="s",private_scope="local",claims=(facts.claims[0].model_copy(update={"value":None}),*facts.claims[1:]))
    with pytest.raises(ValueError,match="unknown fact"):validate_document_references(ResumeDocumentV2.model_validate(raw),null_facts,set())


def test_provider_accepts_bound_alias_but_rejects_unbound_wrong_locale_and_nonproject_name():
    facts, _ = fixture();request=WriterRequest.model_validate({"jd_text":"Fictional job", "resume_locale":"en", "facts":[claim.model_dump(exclude={"evidence"}) for claim in facts.claims]})
    raw=LocalWriterV2().bundle(request)
    experience=next(section for section in raw["job"]["sections"] if section["type"]=="experience")
    entry=experience["entries"][0];entry["role_ref"]="role";entry["display_aliases"]={"role_ref":{"locale":"en","text":"Equipment Engineer"}}
    validate_bundle_output(raw,request)
    bad=deepcopy(raw);next(s for s in bad["job"]["sections"] if s["type"]=="experience")["entries"][0]["role_ref"] = None
    with pytest.raises(ValueError):validate_bundle_output(bad,request)
    bad=deepcopy(raw);next(s for s in bad["job"]["sections"] if s["type"]=="experience")["entries"][0]["display_aliases"]["role_ref"]["locale"]="zh-CN"
    with pytest.raises(ValueError,match="locale"):validate_bundle_output(bad,request)
    bad=deepcopy(raw);next(s for s in bad["job"]["sections"] if s["type"]=="experience")["entries"][0]["name"]="Invented company"
    with pytest.raises(ValueError,match="uncited"):validate_bundle_output(bad,request)


def test_date_alias_requires_bound_correct_type_and_cannot_invent_years():
    facts, raw = fixture()
    raw["sections"][0]["entries"][0]["date_ref"] = "employment"
    raw["sections"][0]["entries"][0]["display_aliases"]["date_ref"] = {"locale": "en", "text": "Sep 2020 – Jun 2024"}
    facts = facts.model_copy(update={"claims": (*facts.claims, Claim(claim_id="employment", fact_type="employment_date", value="2020年9月—2024年6月"))})
    doc = ResumeDocumentV2.model_validate(raw)
    validate_document_references(doc, facts, set())
    assert "Sep 2020 – Jun 2024" in " ".join(text for _, text in resume_blocks(doc, facts, {}, draft=True))
    no_months = deepcopy(raw); no_months["sections"][0]["entries"][0]["display_aliases"]["date_ref"]["text"] = "2020–2024"
    with pytest.raises(ValueError, match="month precision"):
        validate_document_references(ResumeDocumentV2.model_validate(no_months), facts, set())
    forged = deepcopy(raw); forged["sections"][0]["entries"][0]["display_aliases"]["date_ref"]["text"] = "Sep 2020 – Jun 2025"
    with pytest.raises(ValueError, match="invent"):
        validate_document_references(ResumeDocumentV2.model_validate(forged), facts, set())
    wrong = facts.model_copy(update={"claims": (*facts.claims[:-1], facts.claims[-1].model_copy(update={"fact_type":"education_date"}))})
    with pytest.raises(ValueError, match="incompatible"):
        validate_document_references(doc, wrong, set())
    unbound = deepcopy(raw); unbound["sections"][0]["entries"][0]["date_ref"] = None
    with pytest.raises(ValidationError):
        ResumeDocumentV2.model_validate(unbound)


def test_year_range_date_alias_can_reach_review_only_when_exactly_bound():
    from app.generation_v2 import review_bundle_v2
    frozen = FactSnapshot(snapshot_id="s", private_scope="local", claims=(
        Claim(claim_id="date", fact_type="education_date", value="2021年到2024年"),
        Claim(claim_id="school", fact_type="school", value="虚构大学"),
    ))
    class Dated(LocalWriterV2):
        name = "fictional"
        def __init__(self): self.review_inputs = []
        def bundle(self, request):
            raw = super().bundle(request)
            for kind in ("job", "blueprint"):
                edu = next(s for s in raw[kind]["sections"] if s["type"] == "education")
                entry = next(e for e in edu["entries"] if e.get("date_ref"))
                entry["display_aliases"] = {"date_ref": {"locale":"en", "text":"2021–2024"}}
            return raw
        def review(self, payload):
            self.review_inputs.append(payload)
            return {"issues": []}
    provider = Dated()
    args = dict(jd_text="虚构工程岗位", snapshot=frozen, private_header={}, resume_locale="en")
    result = generate_bundle_v2(provider, **args)
    assert review_bundle_v2(provider, result=result, **args)["status"] == "passed"
    assert "2021–2024" in str(provider.review_inputs[0])
    assert "2021–2024" in " ".join(text for _, text in resume_blocks(ResumeDocumentV2.model_validate(result["documents"]["job"]), frozen, {}, draft=True))
    # Identical-looking content is not exempt in ordinary prose or a plan.
    ordinary = deepcopy(result); ordinary["documents"]["job"]["summary"]["text"] = "2021–2024"
    with pytest.raises(ValueError, match="privacy"):
        review_bundle_v2(provider, result=ordinary, **args)
    nested = deepcopy(result); nested["plans"][0]["display_aliases"]={"date_ref":{"text":"2021–2024"}}
    with pytest.raises(ValueError, match="privacy"):
        review_bundle_v2(provider, result=nested, **args)
    # A known private value is never exempt, even if it resembles a year range.
    with pytest.raises(ValueError, match="privacy"):
        review_bundle_v2(provider, result=result, **{**args, "private_header":{"phone":"2021–2024"}})


@pytest.mark.parametrize("alias", ["2024–2021", "2021–2025", "2021–2024 13800000000", "13800000000"])
def test_date_alias_rejects_reversed_invented_and_phone_values(alias):
    from app.provider_contract_v2 import build_writer_request
    facts = FactSnapshot(snapshot_id="s", private_scope="local", claims=(
        Claim(claim_id="date", fact_type="education_date", value="2021年到2024年"),
    ))
    request = build_writer_request(jd_text="虚构岗位", snapshot=facts, private_header={}, resume_locale="en")
    raw = LocalWriterV2().bundle(request)
    for doc in (raw["job"],raw["blueprint"]):
        entry = next(e for s in doc["sections"] if s["type"]=="education" for e in s["entries"] if e.get("date_ref"))
        entry["display_aliases"]={"date_ref":{"locale":"en", "text":alias}}
    with pytest.raises(ValueError):
        validate_bundle_output(raw, request)


def test_year_range_alias_does_not_relax_other_fields_or_unbound_facts():
    facts, raw = fixture()
    raw["sections"][0]["entries"][0]["display_aliases"]["role_ref"]["text"] = "2021–2024"
    # Direct saved documents cannot be treated as a verified translation; model
    # output and review must continue to reject the phone-shaped role alias.
    request = WriterRequest.model_validate({"jd_text":"Fictional job", "resume_locale":"en", "facts":[claim.model_dump(exclude={"evidence"}) for claim in facts.claims]})
    bundle = LocalWriterV2().bundle(request)
    entry = next(e for s in bundle["job"]["sections"] if s["type"]=="experience" for e in s["entries"])
    entry["role_ref"]="role";entry["display_aliases"]={"role_ref":{"locale":"en","text":"2021–2024"}}
    with pytest.raises(ValueError,match="privacy"):
        validate_bundle_output(bundle,request)
