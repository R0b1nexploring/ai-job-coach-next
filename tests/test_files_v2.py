from io import BytesIO

from docx import Document
import pytest

from app.content_v2 import FactSnapshot, ResumeDocumentV2
from app.files_v2 import guide_blocks, render_docx, render_html, resume_blocks


def _unit(identifier, text, purpose):
    return {"unit_id": identifier, "text": text, "authorship": "ai_rewrite", "completion": "unknown", "purpose": purpose}


def _document(kind="job", locale="en"):
    return ResumeDocumentV2.model_validate({
        "kind": kind, "locale": locale, "title": "Engineering profile", "summary": _unit("s", "Summary & context", "summary"),
        "sections": [{"section_id": "sec", "type": "experience", "entries": [{
            "entry_id": "e1", "organization_ref": "company", "role_ref": "role", "date_ref": "date",
            "units": [_unit("u1", "Maintained fictional records <safely>.", "experience")]}]}]})


def _facts():
    return FactSnapshot.model_validate({"snapshot_id": "snap", "private_scope": "local", "claims": [
        {"claim_id": "company", "fact_type": "employer", "value": "Example Ltd"},
        {"claim_id": "role", "fact_type": "job_title", "value": "Engineer"},
        {"claim_id": "date", "fact_type": "employment_date", "value": "2025–2026"}]})


def test_saved_resume_blocks_match_html_and_docx_and_escape_html():
    blocks = resume_blocks(_document(), _facts(), {"name": "A & B", "email": "fiction@example.test"}, draft=True)
    html = render_html(blocks, "en").decode()
    assert "A &amp; B" in html and "&lt;safely&gt;" in html
    assert "DRAFT" in html and "Example Ltd · Engineer · 2025–2026" in html
    doc = Document(BytesIO(render_docx(blocks)))
    paragraphs = [paragraph.text for paragraph in doc.paragraphs]
    assert paragraphs == [("• " if kind == "bullet" else "") + text for kind, text in blocks]
    assert "fiction@example.test" in "\n".join(paragraphs)


def test_guide_uses_own_locale_revision_and_blueprint_notice():
    blocks = guide_blocks({"entries": [], "context_notes": [{"text": "准备一个岗位问题。"}]}, "zh-CN", resume_revision=3, kind="blueprint", draft=False)
    doc_text = "\n".join(p.text for p in Document(BytesIO(render_docx(blocks))).paragraphs)
    assert "对应简历版本: 3" in doc_text
    assert "准备参考" in doc_text and "准备一个岗位问题。" in doc_text
    assert "准备说明" in render_html(blocks, "zh-CN").decode()


def test_missing_bound_fact_does_not_silently_invent_value():
    missing = FactSnapshot.model_validate({"snapshot_id": "s", "private_scope": "local", "claims": []})
    with pytest.raises(ValueError, match="unknown saved claim"):
        resume_blocks(_document(), missing, {}, draft=False)


def test_education_placeholder_and_explicit_adoption():
    assert any("School: ___" in text for _, text in resume_blocks(_document(), _facts(), {}, draft=False))
    data = _document().model_dump(mode="json")
    data["sections"].append({"section_id": "edu", "type": "education", "entries": [{
        "entry_id": "edu1", "school_ref": "school", "units": []}]})
    doc = ResumeDocumentV2.model_validate(data)
    facts = _facts().model_dump(mode="json")
    facts["claims"].append({"claim_id": "school", "fact_type": "school", "value": "Fictional School"})
    facts = FactSnapshot.model_validate(facts)
    blocks = resume_blocks(doc, facts, {"education": "Fictional School"}, draft=False, education_fact_adopted=True)
    assert sum(text == "Fictional School" for _, text in blocks) == 1
    # Without an explicit adoption decision, preserve the private header.
    blocks = resume_blocks(doc, facts, {"education": "Fictional School"}, draft=False)
    assert sum(text == "Fictional School" for _, text in blocks) == 2


def test_empty_structural_entries_keep_education_fill_in_area():
    data = _document().model_dump(mode="json")
    data["sections"] = [
        {"section_id": "exp-empty", "type": "experience", "entries": [{"entry_id": "e-empty", "units": []}]},
        {"section_id": "edu-empty", "type": "education", "entries": [{"entry_id": "d-empty", "units": []}]},
    ]
    doc = ResumeDocumentV2.model_validate(data)
    blocks = resume_blocks(doc, _facts(), {}, draft=True)
    assert sum(text == "Education" for kind, text in blocks if kind == "heading") == 1
    assert any("School: ___" in text for _, text in blocks)
    assert not any(text == "Experience" for kind, text in blocks if kind == "heading")


def test_layout_choices_apply_to_docx_and_html():
    blocks = [("title", "Fictional example")]
    a4 = Document(BytesIO(render_docx(blocks)))
    letter = Document(BytesIO(render_docx(blocks, paper="Letter", page_target=2)))
    assert round(a4.sections[0].page_width.cm, 2) == 21.0
    assert round(letter.sections[0].page_width.cm, 2) == 21.59
    assert b"size:Letter" in render_html(blocks, "en", paper="Letter", page_target=2)
    for options in ({"paper": "Legal"}, {"page_target": 3}, {"page_target": True}):
        with pytest.raises(ValueError):
            render_docx(blocks, **options)


def test_pdf_conversion_rejects_unconfigured_engine_and_invalid_docx():
    from pathlib import Path
    from app.files_v2 import ConversionUnavailable, ConverterConfig, render_pdf

    config = ConverterConfig(Path("/not/a/converter"), Path("/not/a/bundle"), "bad")
    with pytest.raises(ConversionUnavailable):
        render_pdf(b"not a docx", config=config, locale="en")


def test_bundled_mac_conversion_fictional_pdf_and_page_target():
    import hashlib
    import sys
    from pathlib import Path
    from pypdf import PdfReader
    from app.files_v2 import ConverterConfig, render_pdf

    root = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/libreoffice-headless"
    binary = root / "libreoffice/LibreOfficeDev.app/Contents/MacOS/soffice"
    font = Path(__file__).resolve().parents[1] / "assets/fonts/NotoSansCJKsc-Regular.otf"
    if sys.platform != "darwin" or not binary.is_file() or not font.is_file():
        pytest.skip("requires the bundled macOS LibreOffice and fixed font")
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    config = ConverterConfig(binary, root, digest(binary), font, digest(font))
    blocks = [("title", "虚构简历"), ("text", "参与文档整理 and CAD diagrams.")]
    result = render_pdf(render_docx(blocks), config=config, locale="zh-CN")
    assert result.page_count == 1 and not result.exceeds_target
    assert result.layout_status == "not_checked"
    assert "参与文档整理" in "".join(page.extract_text() for page in PdfReader(BytesIO(result.data)).pages)
    long_blocks = blocks + [("bullet", "虚构项目记录示例 " * 12) for _ in range(90)]
    long_result = render_pdf(render_docx(long_blocks, paper="Letter", page_target=1),
                             config=config, locale="zh-CN", paper="Letter", page_target=1)
    assert long_result.page_count > 1 and long_result.exceeds_target
    assert long_result.layout_status == "needs_revision"
    assert long_result.section_lengths
    assert "虚构项目记录示例" in "".join(page.extract_text() for page in PdfReader(BytesIO(long_result.data)).pages)


def test_default_converter_is_fixed_to_bundled_executable_and_font():
    from app.files_v2 import bundled_converter_config
    config = bundled_converter_config()
    assert "libreoffice-headless" in str(config.bundle_root)
    assert config.binary.is_relative_to(config.bundle_root)
    assert len(config.executable_sha256) == len(config.cjk_font_sha256) == 64
