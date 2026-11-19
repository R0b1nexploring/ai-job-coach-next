"""Local document ingestion and deterministic rendering from saved content."""
from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
from html import escape
from io import BytesIO
import os
from pathlib import Path
import re
from threading import RLock
from zipfile import BadZipFile, ZipFile

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, TTFError
from reportlab.platypus import Paragraph, SimpleDocTemplate


FONT_PATHS = (
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "C:/Windows/Fonts/msyh.ttc",
)
FONT_LOCK = RLock()
DOCX_CJK_FONT = "WenQuanYi Micro Hei"
HTML_FONT_STACK = '"WenQuanYi Micro Hei","Noto Sans CJK SC","Microsoft YaHei","PingFang SC",sans-serif'


class ExportFontError(RuntimeError):
    """A readable embedded font is required; callers can retry after configuration."""


@lru_cache(maxsize=8)
def _register_font(path: str, modified: int, size: int) -> tuple[str, frozenset[int]]:
    name = "CoachCJK-" + sha256(f"{path}:{modified}:{size}".encode()).hexdigest()[:12]
    with FONT_LOCK:
        font = TTFont(name, path, subfontIndex=0)
        pdfmetrics.registerFont(font)
    return name, frozenset(code for code, glyph in font.face.charToGlyph.items() if glyph)


def _pdf_font(text: str) -> str:
    configured = os.getenv("COACH_FONT_PATH")
    candidates = (configured,) if configured else FONT_PATHS
    required = {ord(character) for character in text if not character.isspace()}
    for candidate in candidates:
        path = Path(candidate).expanduser()
        try:
            info = path.stat()
            name, supported = _register_font(str(path.resolve()), info.st_mtime_ns, info.st_size)
        except (OSError, ValueError, RuntimeError, TTFError):
            continue
        if required <= supported:
            return name
    # Do not expose document text or configuration paths in diagnostics.
    raise ExportFontError("PDF 字体不可用或缺少所需字符，请配置可嵌入的中文 TrueType 字体")


def _paragraph_text(text: str) -> str:
    return escape(text).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>").replace("\t", "    ")


def _unicode_pdf(data: bytes) -> bytes:
    """ReportLab emits scalar hex for supplementary glyphs; PDF requires UTF-16BE."""
    reader = PdfReader(BytesIO(data))
    for page in reader.pages:
        for reference in page["/Resources"]["/Font"].values():
            font = reference.get_object()
            if "/ToUnicode" not in font:
                continue
            mapping = font["/ToUnicode"]
            source = mapping.get_data()
            corrected = re.sub(
                rb"(<[0-9A-Fa-f]{2}>\s*)<([0-9A-Fa-f]{5,6})>",
                lambda match: match[1] + b"<" + chr(int(match[2], 16)).encode("utf-16-be").hex().upper().encode() + b">",
                source,
            )
            if corrected != source:
                mapping.set_data(corrected)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def extract_material(name: str, data: bytes) -> str:
    if not data or len(data) > 8 * 1024 * 1024:
        raise ValueError("文件为空或超过 8 MB")
    lower = name.lower()
    if lower.endswith(".docx"):
        if not data.startswith(b"PK"):
            raise ValueError("DOCX 文件格式无效")
        try:
            with ZipFile(BytesIO(data)) as archive:
                if len(archive.namelist()) > 200 or sum(item.file_size for item in archive.infolist()) > 20 * 1024 * 1024:
                    raise ValueError("DOCX 内容过大")
            document = Document(BytesIO(data))
            text = "\n".join(p.text for p in document.paragraphs if p.text.strip())
            for table in document.tables:
                text += "\n" + "\n".join(" | ".join(cell.text for cell in row.cells) for row in table.rows)
        except Exception as error:
            if isinstance(error, ValueError):
                raise
            raise ValueError("DOCX 文件无法读取") from None
    elif lower.endswith(".pdf"):
        if not data.startswith(b"%PDF"):
            raise ValueError("PDF 文件格式无效")
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted or len(reader.pages) > 20:
                raise ValueError("PDF 加密或页数超过 20 页")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError("PDF 无法解析，请改用手工粘贴") from None
    elif lower.endswith(".txt"):
        text = data.decode("utf-8-sig", errors="replace")
    else:
        raise ValueError("只支持 DOCX、文本 PDF 和 TXT")
    text = text.strip()
    if len(text) < 10 or "\ufffd" in text[:100]:
        raise ValueError("未提取到可用文字，请手工粘贴或更换文件")
    if len(text) > 20000:
        raise ValueError("文字超过 20000 字，请先精简素材或分段手工粘贴")
    return text


def _profile_header(resume: dict, profile: dict | None) -> tuple[str, str, str]:
    """A saved header takes precedence, including intentionally cleared fields.

    Explicit profile remains a fallback for older standalone renderer callers.
    Missing identity leaves writing space without inventing personal details.
    """
    header = (resume.get("header") or {}) if "header" in resume else (profile or {})
    name = header.get("name") or "姓名：________________"
    contact = "  |  ".join(
        f"{label}：{header.get(key) or blank}"
        for key, label, blank in (
            ("phone", "电话", "____________"),
            ("email", "邮箱", "________________"),
            ("location", "所在城市", "________"),
        )
    )
    education = header.get("education") or "学校：________________    专业：____________\n学历：________    就读时间：________________"
    return name, contact, education


def render_html(resume: dict, footer: str | None = None, profile: dict | None = None) -> bytes:
    heading, contact, education = _profile_header(resume, profile)
    summary = escape(resume["summary"])
    items = "".join(f"<li>{escape(item['text'])}</li>" for item in resume["items"])
    note = f'<footer class="export-note">{escape(footer)}</footer>' if footer else ""
    subtitle = f'<p class="intent">{escape(resume["title"])}</p>'
    contact_html = f'<p class="contact">{escape(contact)}</p>'
    education_html = f'<h2>教育背景</h2><p>{escape(education)}</p>'
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{escape(heading)}</title>
<style>@page{{size:A4;margin:18mm}}body{{font:10.5pt/1.6 {HTML_FONT_STACK};color:#182838;max-width:800px;margin:auto;overflow-wrap:anywhere}}h1{{font-size:20pt;margin:0 0 2mm}}h2{{font-size:12pt;margin:8mm 0 3mm;break-after:avoid}}p,li{{white-space:pre-wrap}}li{{margin:0 0 4mm}}.contact{{color:#5a6b7b;font-size:9.5pt;margin:0 0 6mm}}.intent{{color:#34465a;font-size:11pt;margin:0 0 6mm}}.export-note{{margin-top:10mm;font-size:8.5pt;color:#8a94a0;border-top:1px solid #d5dce3;padding-top:2mm}}@media print{{body{{max-width:none}}}}</style>
<h1>{escape(heading)}</h1>{contact_html}{subtitle}<p>{summary}</p>{education_html}<h2>项目与能力</h2><ol>{items}</ol>{note}</html>'''.encode("utf-8")


def render_guide_html(guide: dict, resume: dict) -> bytes:
    title = escape(resume["title"] + " 面试指南")
    binding = ""
    if isinstance(resume.get("revision"), int) and resume["revision"] > 0:
        binding = f'<p class="version">对应简历第 {resume["revision"]} 版'
        if isinstance(guide.get("revision"), int) and guide["revision"] > 0:
            binding += f' · 面试指南第 {guide["revision"]} 版'
        binding += "</p>"
    entries = "".join(
        "<section><h2>" + escape(entry["question"]) + "</h2>"
        + "".join(f"<p><strong>{label}：</strong>{escape(entry[key])}</p>" for label, key in [
            ("回答思路", "answer_outline"), ("贡献边界", "contribution_boundary"),
            ("验证方式", "verification"), ("准备动作", "practice_step")]) + "</section>"
        for entry in guide["entries"]
    )
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{title}</title>
<style>@page{{size:A4;margin:18mm}}body{{font:11pt/1.7 {HTML_FONT_STACK};max-width:800px;margin:auto;color:#182838;overflow-wrap:anywhere}}h1{{font-size:20pt}}h2{{font-size:13pt;margin-top:10mm;break-after:avoid}}p{{white-space:pre-wrap}}section{{break-inside:avoid}}</style>
<h1>{title}</h1>{binding}{entries}</html>'''.encode("utf-8")


def render_docx(resume: dict, footer: str | None = None, profile: dict | None = None) -> bytes:
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(1.8)
    section.left_margin = section.right_margin = Cm(2)
    for name, size in (("Normal", 10.5), ("Title", 20), ("Heading 1", 12), ("List Number", 10.5)):
        style = document.styles[name]
        style.font.name = "DejaVu Sans"
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor.from_string("000000" if name in {"Title", "Heading 1"} else "253444")
        fonts = style.element.get_or_add_rPr().rFonts
        fonts.set(qn("w:eastAsia"), DOCX_CJK_FONT)
        for attribute in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            fonts.attrib.pop(qn("w:" + attribute), None)
        style.paragraph_format.widow_control = True
        style.paragraph_format.line_spacing = Pt(28 if name == "Title" else 19 if name == "Heading 1" else 17)
        style.paragraph_format.space_after = Pt(8)
    document.styles["Heading 1"].paragraph_format.space_before = Pt(12)
    title_properties = document.styles["Title"].element.get_or_add_pPr()
    for border in title_properties.findall(qn("w:pBdr")):
        title_properties.remove(border)
    heading, contact, education = _profile_header(resume, profile)
    document.add_heading(heading, 0)
    line = document.add_paragraph()
    run = line.add_run(contact)
    run.font.size = Pt(9.5)
    run.font.color.rgb = RGBColor.from_string("5A6B7B")
    intent = document.add_paragraph()
    run = intent.add_run(resume["title"])
    run.font.size = Pt(11)
    run.font.color.rgb = RGBColor.from_string("34465A")
    document.add_paragraph(resume["summary"])
    document.add_heading("教育背景", 1)
    document.add_paragraph(education)
    document.add_heading("项目与能力", 1)
    for item in resume["items"]:
        document.add_paragraph(item["text"], style="List Number")
    if footer:
        note = document.add_paragraph()
        run = note.add_run(footer)
        run.font.size = Pt(8.5)
        run.font.color.rgb = RGBColor.from_string("8A94A0")
    out = BytesIO()
    document.save(out)
    return out.getvalue()


def render_pdf(resume: dict, footer: str | None = None, profile: dict | None = None) -> bytes:
    heading, contact, education = _profile_header(resume, profile)
    content = [heading, contact, resume["title"], resume["summary"], "教育背景", education, "项目与能力"] + [item["text"] for item in resume["items"]]
    if footer:
        content.append(footer)
    font_name = _pdf_font("\n".join(content) + "0123456789.·")
    regular = ParagraphStyle("regular", fontName=font_name, fontSize=10.5, leading=17, spaceAfter=8, alignment=TA_LEFT, textColor=colors.HexColor("#253444"), wordWrap="CJK", splitLongWords=True, allowWidows=False, allowOrphans=False)
    title = ParagraphStyle("title", parent=regular, fontSize=20, leading=28, spaceAfter=22)
    section = ParagraphStyle("section", parent=regular, fontSize=12, leading=19, spaceBefore=12, spaceAfter=10, keepWithNext=True)
    contact_style = ParagraphStyle("contact", parent=regular, fontSize=9.5, leading=14, spaceAfter=10, textColor=colors.HexColor("#5A6B7B"))
    intent = ParagraphStyle("intent", parent=regular, fontSize=11, leading=16, spaceAfter=10, textColor=colors.HexColor("#34465A"))
    out = BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=50, rightMargin=50, topMargin=50, bottomMargin=50, initialFontName=font_name, lang="zh-CN")
    story = [Paragraph(_paragraph_text(heading), title)]
    story.append(Paragraph(_paragraph_text(contact), contact_style))
    story.append(Paragraph(_paragraph_text(resume["title"]), intent))
    story.append(Paragraph(_paragraph_text(resume["summary"]), regular))
    story.append(Paragraph("教育背景", section))
    story.append(Paragraph(_paragraph_text(education), regular))
    story.append(Paragraph("项目与能力", section))
    for index, item in enumerate(resume["items"], 1):
        story.append(Paragraph(f"{index}. " + _paragraph_text(item["text"]), regular))
    if footer:
        note = ParagraphStyle("export-note", parent=regular, fontSize=8.5, leading=12, spaceBefore=16, textColor=colors.HexColor("#8A94A0"))
        story.append(Paragraph(_paragraph_text(footer), note))
    doc.build(story)
    result = out.getvalue()
    return _unicode_pdf(result) if any(ord(character) > 0xFFFF for text in content for character in text) else result
