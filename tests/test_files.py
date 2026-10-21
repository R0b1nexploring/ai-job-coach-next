from copy import deepcopy
from html.parser import HTMLParser
from io import BytesIO
import re

from docx import Document
from docx.oxml.ns import qn
from fastapi.testclient import TestClient
from pypdf import PdfReader
import pytest

from app import files


def long_resume() -> dict:
    return {
        "title": "虚构简历：机械与软件设计",
        "summary": "这是虚构导出验收材料。使用 CAD / Python，比较 α、β 和 μm 单位；数值为设计指标。\n不代表任何真实个人经历。",
        "items": [
            {"id": f"synthetic-{index}", "text": (
                f"虚构项目 {index:02d}：整理装配结构与 BOM，记录本人负责的接口、输入和产物。"
                + "用 CAD 比较结构方案，编写 Python 检查表，核对尺寸 ±0.2 mm、温度 25 ℃ 和版本依据；保存图纸与验证记录，区分团队任务和本人贡献。" * 3
                + f"\n检查点 {index:02d}：保留完整中文与 EnglishIdentifierWithoutSpaces{'X' * 55}，不自动删字。"
            ), "verification_status": "ai_generated", "achieved": False}
            for index in range(1, 9)
        ],
    }


class VisibleContent(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag in {"h1", "h2", "p", "li"}:
            self.current = ""

    def handle_data(self, data):
        if self.current is not None:
            self.current += data

    def handle_endtag(self, tag):
        if tag in {"h1", "h2", "p", "li"} and self.current is not None:
            self.parts.append(self.current)
            self.current = None


def compact(text):
    return re.sub(r"\s+", "", text)


def test_long_exports_preserve_same_saved_content_and_embed_cjk():
    resume = long_resume()
    before = deepcopy(resume)
    expected = [resume["title"], resume["summary"], "项目与能力"] + [item["text"] for item in resume["items"]]

    html = VisibleContent()
    html.feed(files.render_html(resume).decode())
    docx = Document(BytesIO(files.render_docx(resume)))
    assert html.parts[4:] == expected
    assert [paragraph.text for paragraph in docx.paragraphs][4:] == expected

    pdf = PdfReader(BytesIO(files.render_pdf(resume)))
    assert 2 <= len(pdf.pages) <= 4
    extracted = compact("\n".join(page.extract_text() for page in pdf.pages))
    cursor = 0
    for text in expected:
        cursor = extracted.index(compact(text), cursor) + len(compact(text))
    embedded = []
    for page in pdf.pages:
        assert float(page.mediabox.width) == pytest.approx(595.28, abs=1)
        assert float(page.mediabox.height) == pytest.approx(841.89, abs=1)
        for reference in page["/Resources"]["/Font"].values():
            font = reference.get_object()
            assert "/FontDescriptor" in font
            assert "/FontFile2" in font["/FontDescriptor"]
            assert font["/ToUnicode"].get_data()
            embedded.append(font)
    assert embedded
    for name in ("Normal", "Title", "Heading 1", "List Number"):
        assert docx.styles[name].element.rPr.rFonts.get(qn("w:eastAsia")) == files.DOCX_CJK_FONT
        assert docx.styles[name].element.rPr.rFonts.get(qn("w:eastAsiaTheme")) is None
    assert docx.styles["Title"].element.pPr.find(qn("w:pBdr")) is None
    assert resume == before


def test_html_escapes_saved_text_and_pdf_preserves_symbols():
    text = '虚构文本 <script> & "测试"；café / αβ / μm / ≥ / ±。'
    resume = {"title": "虚构字符验收", "summary": "所有内容都是虚构测试材料。", "items": [{"text": text}]}
    html = files.render_html(resume).decode()
    assert "<script>" not in html
    visible = VisibleContent()
    visible.feed(html)
    assert visible.parts[-1] == text
    reader = PdfReader(BytesIO(files.render_pdf(resume)))
    assert compact(text) in compact(reader.pages[0].extract_text())


def test_explicit_missing_font_does_not_fall_back_or_return_blank_pdf(tmp_path, monkeypatch):
    from app import api

    resume = long_resume()
    monkeypatch.setenv("COACH_FONT_PATH", str(tmp_path / "missing.ttf"))
    with pytest.raises(files.ExportFontError, match="PDF 字体不可用"):
        files.render_pdf(resume)
    monkeypatch.setattr(api, "get_batch", lambda *args, **kwargs: {"resumes": {"job": resume}})
    response = TestClient(api.app).get("/api/batches/synthetic/resumes/job/download/pdf")
    assert response.status_code == 503
    assert response.json()["detail"] == "文件生成失败，请稍后重试"
    assert resume["items"][0]["verification_status"] == "ai_generated"


def test_missing_glyph_and_no_discovered_font_fail_explicitly(monkeypatch):
    resume = {"title": "虚构字符验收", "summary": "仅用于测试。", "items": [{"text": "不支持的字符：\U0010ffff"}]}
    with pytest.raises(files.ExportFontError):
        files.render_pdf(resume)
    monkeypatch.delenv("COACH_FONT_PATH", raising=False)
    monkeypatch.setattr(files, "FONT_PATHS", ())
    with pytest.raises(files.ExportFontError):
        files.render_pdf(long_resume())


def test_supplementary_unicode_is_preserved_or_rejected_before_rendering():
    # Some CJK fonts include extension B; fonts without it must fail, never drop it.
    text = "虚构扩展汉字：\U0002000b。"
    resume = {"title": "虚构 Unicode 验收", "summary": "仅用于字体测试。", "items": [{"text": text}]}
    try:
        content = files.render_pdf(resume)
    except files.ExportFontError:
        return
    reader = PdfReader(BytesIO(content))
    assert compact(text) in compact("".join(page.extract_text() for page in reader.pages))


def test_guide_export_marks_original_bound_versions():
    guide = {"revision": 2, "entries": []}
    resume = {"title": "虚构求职版", "revision": 3}
    html = files.render_guide_html(guide, resume).decode()
    assert "对应简历第 3 版 · 面试指南第 2 版" in html
    assert "revision" not in html


def test_corrupt_docx_and_overlong_text_are_controlled_errors():
    from zipfile import ZipFile
    source = Document()
    source.add_paragraph("完全虚构上传验收文本，仅用于损坏解析测试。")
    buffer = BytesIO()
    source.save(buffer)
    broken = BytesIO()
    with ZipFile(BytesIO(buffer.getvalue())) as original, ZipFile(broken, "w") as archive:
        for name in original.namelist():
            archive.writestr(name, b"<broken" if name == "word/document.xml" else original.read(name))
    with pytest.raises(ValueError, match="DOCX 文件无法读取"):
        files.extract_material("fictional.docx", broken.getvalue())
    with pytest.raises(ValueError, match="20000"):
        files.extract_material("fictional.txt", ("虚构" * 10001).encode())


@pytest.mark.parametrize("filled", [False, True])
def test_manual_header_precedes_content_in_all_exports(filled):
    resume = {"title": "虚构求职版", "summary": "完全虚构的项目说明。", "items": [{"text": "虚构课程设计，保留图纸与修改记录。"}]}
    if filled:
        resume["header"] = {"name": "虚构同学", "phone": "00000000000", "email": "fictional@example.invalid", "location": "虚构城市", "education": "虚构理工大学 | 机械工程 | 本科 | 2022-2026\n课程仅用于测试 <script> & Unicode α。"}
    original = deepcopy(resume)
    html = VisibleContent()
    raw_html = files.render_html(resume).decode()
    html.feed(raw_html)
    docx = Document(BytesIO(files.render_docx(resume)))
    paragraphs = [p.text for p in docx.paragraphs]
    assert html.parts == paragraphs
    assert html.parts[2] == "教育经历"
    assert html.parts[4] == resume["title"]
    assert html.parts[5] == resume["summary"]
    pdf = PdfReader(BytesIO(files.render_pdf(resume)))
    text = compact("\n".join(page.extract_text() for page in pdf.pages))
    cursor = 0
    for part in html.parts:
        cursor = text.index(compact(part), cursor) + len(compact(part))
    if filled:
        assert paragraphs[0] == resume["header"]["name"]
        assert paragraphs[3] == resume["header"]["education"]
        assert "________________" not in "".join(paragraphs)
        assert "<script>" not in raw_html
    else:
        assert paragraphs[0].startswith("姓名：")
        assert all(label in paragraphs[1] for label in ["电话：", "邮箱：", "所在城市："])
        assert all(label in paragraphs[3] for label in ["学校：", "专业：", "学历：", "就读时间："])
        assert "fictional@example.invalid" not in "".join(paragraphs)
    assert resume == original


def test_long_education_and_contact_wrap_without_losing_saved_text():
    resume = long_resume()
    resume["header"] = {"name": "虚构同学", "email": "fictional-" + "x" * 120 + "@example.invalid", "education": "虚构教育记录：" + "仅为排版测试，学校与课程均为虚构。" * 70}
    pdf = PdfReader(BytesIO(files.render_pdf(resume)))
    text = compact("\n".join(page.extract_text() for page in pdf.pages))
    assert compact(resume["header"]["email"]) in text
    assert compact(resume["header"]["education"]) in text
    assert compact(resume["items"][-1]["text"]) in text
