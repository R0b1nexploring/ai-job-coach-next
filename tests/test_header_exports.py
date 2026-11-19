"""Manual identity stays with each saved resume, including an empty header."""
from copy import deepcopy
from html.parser import HTMLParser
from io import BytesIO
import re

from docx import Document
from pypdf import PdfReader
import pytest

from app.files import render_docx, render_html, render_pdf


class ExportText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        self.in_style = tag == "style" or self.in_style

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if not self.in_style:
            self.parts.append(data)


def export_text(format, resume, **kwargs):
    if format == "html":
        parser = ExportText()
        parser.feed(render_html(resume, **kwargs).decode())
        text = "\n".join(parser.parts)
    elif format == "docx":
        text = "\n".join(p.text for p in Document(BytesIO(render_docx(resume, **kwargs))).paragraphs)
    else:
        text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(render_pdf(resume, **kwargs))).pages)
    return re.sub(r"\s+", "", text)


@pytest.fixture
def resume():
    return {
        "title": "虚构机械结构设计求职版",
        "summary": "完全虚构的导出验收简介。",
        "items": [{"text": "完全虚构的装配图整理项目。"}],
    }


@pytest.mark.parametrize("format", ["html", "docx", "pdf"])
def test_empty_header_keeps_identity_and_education_writing_space(format, resume):
    resume["header"] = {}
    before = deepcopy(resume)
    text = export_text(format, resume, profile={"name": "不可回填的批次姓名", "education": "不可回填的批次学校"})
    for label in ["姓名", "电话", "邮箱", "所在城市", "学校", "专业", "学历", "就读时间"]:
        assert label + "：" in text
    assert "________________" in text
    assert "不可回填" not in text
    assert "虚构机械结构设计求职版" in text
    assert resume == before


@pytest.mark.parametrize("format", ["html", "docx", "pdf"])
def test_header_overrides_legacy_profile_and_partial_fields_keep_blanks(format, resume):
    resume["header"] = {"name": "虚构甲", "location": "虚构城市", "education": "虚构大学\n机械工程 本科"}
    text = export_text(format, resume, profile={"name": "过时姓名", "phone": "过时电话", "education": "过时学校"})
    assert "虚构甲" in text
    assert "所在城市：虚构城市" in text
    assert "电话：____________" in text
    assert "邮箱：________________" in text
    assert "虚构大学机械工程本科" in text
    assert "过时" not in text


@pytest.mark.parametrize("format", ["html", "docx", "pdf"])
def test_explicit_profile_is_only_fallback_when_resume_has_no_header(format, resume):
    text = export_text(format, resume, profile={"name": "兼容调用姓名", "phone": "00000000000"})
    assert "兼容调用姓名" in text and "电话：00000000000" in text
    assert "邮箱：________________" in text and "学校：________________" in text


@pytest.mark.parametrize("format", ["html", "docx", "pdf"])
def test_revision_header_does_not_drift_and_blueprint_footer_survives(format, resume):
    old = {**resume, "header": {"name": "历史虚构姓名", "education": "历史虚构学校"}}
    current = {**resume, "header": {"name": "当前虚构姓名", "education": "当前虚构学校"}}
    footer = "蓝图 Plus 含尚未完成的项目规划，仅供准备参考。"
    old_text = export_text(format, old, footer=footer, profile=current["header"])
    current_text = export_text(format, current, footer=footer)
    assert "历史虚构姓名" in old_text and "历史虚构学校" in old_text
    assert "当前虚构姓名" not in old_text and "当前虚构学校" not in old_text
    assert "当前虚构姓名" in current_text and "当前虚构学校" in current_text
    assert footer.replace(" ", "") in old_text and footer.replace(" ", "") in current_text


def test_html_escapes_manual_identity_fields(resume):
    resume["header"] = {"name": '<script>虚构姓名</script>', "education": '虚构学校 <b>专业</b> & 备注'}
    html = render_html(resume).decode()
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;虚构姓名&lt;/script&gt;" in html
    assert "虚构学校 &lt;b&gt;专业&lt;/b&gt; &amp; 备注" in html
