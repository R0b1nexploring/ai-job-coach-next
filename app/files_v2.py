"""Local HTML and DOCX renderers for already saved v2 snapshots.

Both formats consume the same ordered blocks. This module does not publish a
revision or declare file QA passed. PDF conversion is a separate, configured
LibreOffice step and must not fall back to the v1 renderer.
"""
from __future__ import annotations

from html import escape
from io import BytesIO
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree

from pypdf import PdfReader

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from app.content_v2 import FactSnapshot, ResumeDocumentV2, validate_entry_claim_types, validate_display_alias_values

_LABELS = {
    "zh-CN": {"experience": "工作经历", "projects": "项目经历", "skills": "技能", "education": "教育背景",
              "name": "姓名：________________", "education_blank": "学校：________________    专业：____________\n学历：________    就读时间：________________", "phone": "电话", "email": "邮箱", "location": "所在城市",
              "draft": "草稿 · 请核对后使用", "blueprint": "蓝图 Plus · 准备参考", "guide": "面试指南",
              "outline": "回答思路", "boundary": "贡献边界", "verification": "验证方式", "practice": "准备动作",
              "notes": "准备说明", "revision": "对应简历版本"},
    "en": {"experience": "Experience", "projects": "Projects", "skills": "Skills", "education": "Education",
           "name": "Name: ________________", "education_blank": "School: ________________    Major: ____________\nDegree: ____________    Dates: ________________", "phone": "Phone", "email": "Email", "location": "Location",
           "draft": "DRAFT · Review before use", "blueprint": "Blueprint Plus · Preparation reference", "guide": "Interview Guide",
           "outline": "Answer outline", "boundary": "Contribution boundary", "verification": "Verification", "practice": "Practice",
           "notes": "Preparation notes", "revision": "Resume revision"},
}


def _value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _has_entry_content(entry, claims: dict) -> bool:
    """An empty structural entry is not completed education or experience."""
    if _value(entry.name) or _value(entry.category) or any(_value(unit.text) for unit in entry.units):
        return True
    for ref in (entry.entity_ref, entry.organization_ref, entry.role_ref, entry.school_ref,
                entry.major_ref, entry.degree_ref, entry.date_ref):
        if ref is not None:
            if ref not in claims:
                raise ValueError("entry references an unknown saved claim")
            if _value(claims[ref].value):
                return True
    return False


def resume_blocks(document: ResumeDocumentV2, facts: FactSnapshot, header: dict, *, draft: bool,
                  education_fact_adopted: bool = False) -> list[tuple[str, str]]:
    """Resolve display facts from the explicitly bound immutable snapshot."""
    labels = _LABELS[document.locale]
    claims = {claim.claim_id: claim for claim in facts.claims}
    blocks = [("title", _value(header.get("name")) or labels["name"])]
    contacts = [f"{labels[key]}: {_value(header.get(key)) or '________'}" for key in ("phone", "email", "location")]
    blocks.append(("muted", "  |  ".join(contacts)))
    if draft:
        blocks.append(("notice", labels["draft"]))
    if document.kind == "blueprint":
        blocks.append(("notice", labels["blueprint"]))
    blocks.extend([("subtitle", document.title), ("text", document.summary.text)])
    # Legacy header education is private display data, never a model fact.
    has_education = any(section.type == "education" and any(_has_entry_content(entry, claims) for entry in section.entries)
                        for section in document.sections)
    if _value(header.get("education")) and not (education_fact_adopted and has_education):
        blocks.extend([("heading", labels["education"]), ("text", header["education"])])
    elif not _value(header.get("education")) and not has_education:
        blocks.extend([("heading", labels["education"]), ("text", labels["education_blank"])])
    for section in document.sections:
        if not any(_has_entry_content(entry, claims) for entry in section.entries):
            continue
        blocks.append(("heading", labels[section.type]))
        for entry in section.entries:
            if not _has_entry_content(entry, claims):
                continue
            parts = [value for value in (entry.name, entry.category) if value]
            if entry.display_aliases:
                validate_entry_claim_types(entry, section.type, {key: claim.fact_type for key, claim in claims.items()})
                validate_display_alias_values(entry, {key: claim.value for key, claim in claims.items()})
            for field in ("entity_ref", "organization_ref", "role_ref", "school_ref", "major_ref", "degree_ref", "date_ref"):
                ref = getattr(entry, field)
                if ref is not None:
                    if ref not in claims:
                        raise ValueError("entry references an unknown saved claim")
                    value = claims[ref].value
                    alias = entry.display_aliases.get(field)
                    if alias and not _value(value):
                        raise ValueError("display alias cannot replace an unknown saved fact")
                    if alias and alias.locale == document.locale:
                        value = alias.text
                    if value and value not in parts:
                        parts.append(value)
            if parts:
                blocks.append(("entry", " · ".join(parts)))
            blocks.extend(("bullet", unit.text) for unit in entry.units)
    return blocks


def guide_blocks(guide: dict, locale: str, *, resume_revision: int, kind: str, draft: bool) -> list[tuple[str, str]]:
    """Render only the given guide snapshot and its explicit resume revision."""
    if locale not in _LABELS or kind not in {"job", "blueprint"} or resume_revision < 1:
        raise ValueError("invalid guide binding")
    labels = _LABELS[locale]
    blocks = [("title", labels["guide"]), ("muted", f"{labels['revision']}: {resume_revision}")]
    if draft:
        blocks.append(("notice", labels["draft"]))
    if kind == "blueprint":
        blocks.append(("notice", labels["blueprint"]))
    for entry in guide.get("entries", []):
        blocks.append(("heading", entry["question"]))
        for key, label in (("answer_outline", "outline"), ("contribution_boundary", "boundary"),
                           ("verification", "verification"), ("practice_step", "practice")):
            value = _value(entry.get(key))
            if value:
                blocks.append(("text", f"{labels[label]}: {value}"))
    notes = guide.get("context_notes", [])
    if notes:
        blocks.append(("heading", labels["notes"]))
        blocks.extend(("bullet", note["text"]) for note in notes)
    return blocks


def render_html(blocks: list[tuple[str, str]], locale: str, *, paper: str = "A4", page_target: int = 1) -> bytes:
    if locale not in _LABELS:
        raise ValueError("unsupported locale")
    _validate_layout(paper, page_target)
    tags = {"title": "h1", "subtitle": "h2", "heading": "h2", "entry": "h3",
            "text": "p", "muted": "p", "notice": "p", "bullet": "p"}
    content = []
    for kind, value in blocks:
        tag = tags[kind]
        prefix = "• " if kind == "bullet" else ""
        content.append(f'<{tag} class="{kind}">{prefix}{escape(value)}</{tag}>')
    return (f'<!doctype html><html lang="{locale}"><head><meta charset="utf-8"><title>Document</title>'
            f'<style>@page{{size:{paper};margin:18mm}}body{{font:10.5pt/1.5 sans-serif;max-width:800px;margin:auto;'
            'color:#182838;overflow-wrap:anywhere}h1{font-size:20pt;margin:0 0 3mm}'
            'h2{font-size:12pt;margin:6mm 0 2mm;break-after:avoid}h3{font-size:10.5pt;margin:3mm 0 1mm;break-after:avoid}'
            'p{margin:1mm 0;white-space:pre-wrap}.muted,.notice{font-size:9pt;color:#506274}.bullet{padding-left:3mm}</style>'
            '</head><body>' + "".join(content) + '</body></html>').encode("utf-8")


def _validate_layout(paper: str, page_target: int) -> None:
    if paper not in {"A4", "Letter"} or type(page_target) is not int or page_target not in (1, 2):
        raise ValueError("unsupported paper or page target")


def render_docx(blocks: list[tuple[str, str]], *, paper: str = "A4", page_target: int = 1) -> bytes:
    _validate_layout(paper, page_target)
    document = Document()
    page = document.sections[0]
    page.page_width, page.page_height = (Cm(21), Cm(29.7)) if paper == "A4" else (Cm(21.59), Cm(27.94))
    page.top_margin = page.bottom_margin = Cm(1.8)
    page.left_margin = page.right_margin = Cm(2)
    for name, size in (("Normal", 10.5), ("Title", 20), ("Heading 1", 12), ("Heading 2", 10.5)):
        style = document.styles[name]
        style.font.name, style.font.size = "DejaVu Sans", Pt(size)
        fonts = style.element.get_or_add_rPr().rFonts
        fonts.set(qn("w:eastAsia"), "Noto Sans CJK SC")
        for attribute in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            fonts.attrib.pop(qn("w:" + attribute), None)
        style.paragraph_format.space_after = Pt(4)
    for kind, value in blocks:
        if kind == "title":
            document.add_paragraph(value, style="Title")
        elif kind in {"heading", "subtitle"}:
            document.add_paragraph(value, style="Heading 1")
        elif kind == "entry":
            document.add_paragraph(value, style="Heading 2")
        else:
            paragraph = document.add_paragraph(("• " if kind == "bullet" else "") + value)
            if kind in {"notice", "muted"}:
                for run in paragraph.runs:
                    run.font.size = Pt(9)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


class ConversionUnavailable(RuntimeError):
    """Conversion is unavailable or failed; the source DOCX remains usable."""


@dataclass(frozen=True)
class ConverterConfig:
    # The exact executable and its bundle directory must be explicitly configured.
    binary: Path
    bundle_root: Path
    executable_sha256: str
    cjk_font: Path | None = None
    cjk_font_sha256: str | None = None
    timeout_seconds: int = 45
    alternate_binary_hashes: tuple[str, ...] = ()


def bundled_converter_config() -> ConverterConfig:
    """Return the fixed, verified developer runtime; absence fails at conversion."""
    root = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/libreoffice-headless"
    return ConverterConfig(
        binary=root / "libreoffice/LibreOfficeDev.app/Contents/MacOS/soffice",
        bundle_root=root,
        executable_sha256="d52ad6b94d72ff646f39812dc4ae2f44a110744acedfb09d6195d1c0fb1b0f02",
        # Signed macOS arm64 workspace runtime 26.1112.11011.
        alternate_binary_hashes=("1c1576937c83095ab0475f8e448fec3a4e26b636baaa2466d04b2fe8c6d6cf93",),
        cjk_font=Path(__file__).resolve().parents[1] / "assets/fonts/NotoSansCJKsc-Regular.otf",
        cjk_font_sha256="2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b",
    )


@dataclass(frozen=True)
class PdfResult:
    data: bytes
    page_count: int
    page_target: int
    exceeds_target: bool
    section_lengths: tuple[tuple[str, int], ...]

    @property
    def layout_status(self) -> str:
        # Exceeding the chosen limit is never success. Even within the limit,
        # glyphs and page boundaries still need the separately specified QA.
        return "needs_revision" if self.exceeds_target else "not_checked"


def render_pdf(docx: bytes, *, config: ConverterConfig, locale: str,
               paper: str = "A4", page_target: int = 1) -> PdfResult:
    """Convert DOCX with the explicitly pinned bundled engine, fail closed.

    Currently only macOS sandbox-exec is supported. The caller must supply
    DOCX from the matching saved revision. This reports page count, not QA.
    """
    _validate_layout(paper, page_target)
    if locale not in _LABELS or not 1 <= config.timeout_seconds <= 120:
        raise ValueError("unsupported locale or timeout")
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise ConversionUnavailable("network-isolated bundled conversion unavailable")
    try:
        binary = config.binary.resolve(strict=True)
        root = config.bundle_root.resolve(strict=True)
        binary.relative_to(root)
        trusted_root = (Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/libreoffice-headless").resolve(strict=True)
        if root != trusted_root:
            raise ValueError()
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        if not binary.is_file() or not os.access(binary, os.X_OK) or digest not in (config.executable_sha256, *config.alternate_binary_hashes):
            raise ValueError()
        if config.cjk_font is None or config.cjk_font_sha256 is None:
            raise ValueError()
        cjk_bytes = config.cjk_font.read_bytes()
        if len(cjk_bytes) > 40 * 1024 * 1024 or hashlib.sha256(cjk_bytes).hexdigest() != config.cjk_font_sha256:
            raise ValueError()
        if len(docx) > 10 * 1024 * 1024:
            raise ValueError()
        # Fail closed on relationships capable of fetching external objects.
        with ZipFile(BytesIO(docx)) as archive:
            if len(archive.infolist()) > 2000 or sum(info.file_size for info in archive.infolist()) > 30 * 1024 * 1024:
                raise ValueError()
            for info in archive.infolist():
                if info.filename.lower().endswith(".rels"):
                    rels = ElementTree.fromstring(archive.read(info))
                    if any(node.attrib.get("TargetMode", "").lower() == "external" for node in rels.iter()):
                        raise ValueError()
        document = Document(BytesIO(docx))
        expected_size = (21.0, 29.7) if paper == "A4" else (21.59, 27.94)
        if any(abs(section.page_width.cm - expected_size[0]) > 0.02 or
               abs(section.page_height.cm - expected_size[1]) > 0.02 for section in document.sections):
            raise ValueError()
        has_cjk = any(any("\u3400" <= c <= "\u9fff" for c in paragraph.text)
                      for paragraph in document.paragraphs)
        section_lengths: list[tuple[str, int]] = []
        current_heading = ""
        current_length = 0
        for paragraph in document.paragraphs:
            if paragraph.style.name == "Heading 1":
                if current_heading or current_length:
                    section_lengths.append((current_heading, current_length))
                current_heading, current_length = paragraph.text, 0
            else:
                current_length += len(paragraph.text)
        if current_heading or current_length:
            section_lengths.append((current_heading, current_length))
    except (ValueError, OSError, BadZipFile, ElementTree.ParseError, KeyError):
        raise ConversionUnavailable("invalid or untrusted converter input") from None
    try:
        with tempfile.TemporaryDirectory(prefix="jobcoach-v2-") as temporary:
            directory = Path(temporary)
            source = directory / "document.docx"
            source.write_bytes(docx)
            profile = (directory / "profile").as_uri()
            font_dir = directory / "fonts"
            font_dir.mkdir()
            (font_dir / "NotoSansCJKsc-Regular.otf").write_bytes(cjk_bytes)
            latin_font = root / "libreoffice/LibreOfficeDev.app/Contents/Resources/fonts/truetype/DejaVuSans.ttf"
            (font_dir / "DejaVuSans.ttf").write_bytes(latin_font.read_bytes())
            font_config = directory / "fonts.conf"
            font_config.write_text(
                '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
                f'<dir>{escape(str(font_dir))}</dir><cachedir>{escape(str(directory / "cache"))}</cachedir></fontconfig>',
                encoding="utf-8",
            )
            # Network is denied for the entire child tree; CPU and output file
            # size are bounded in the child shell, and wall time below.
            cmd = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)",
                   "/bin/sh", "-c", 'ulimit -t 30; ulimit -f 51200; exec "$@"', "sh",
                   str(binary), f"-env:UserInstallation={profile}", "--headless", "--convert-to",
                   "pdf:writer_pdf_Export", "--outdir", str(directory), str(source)]
            env = {"HOME": str(directory), "TMPDIR": str(directory), "PATH": "/usr/bin:/bin",
                   "FONTCONFIG_FILE": str(font_config),
                   "LANG": "en_US.UTF-8" if locale == "en" else "zh_CN.UTF-8", "LC_ALL": "en_US.UTF-8" if locale == "en" else "zh_CN.UTF-8"}
            process = subprocess.Popen(cmd, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                status = process.wait(timeout=config.timeout_seconds)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise ConversionUnavailable("conversion timed out") from None
            output = directory / "document.pdf"
            if status != 0 or not output.is_file() or output.stat().st_size > 25 * 1024 * 1024:
                raise ConversionUnavailable("conversion failed")
            data = output.read_bytes()
        reader = PdfReader(BytesIO(data))
        count = len(reader.pages)
        if count < 1:
            raise ValueError()
        if has_cjk:
            font_names = [str(font.get_object().get("/BaseFont", "")) for page in reader.pages
                          for font in page.get("/Resources", {}).get("/Font", {}).values()]
            if not any("NotoSansCJKsc" in name for name in font_names):
                raise ValueError()
        return PdfResult(data=data, page_count=count, page_target=page_target, exceeds_target=count > page_target,
                         section_lengths=tuple(section_lengths))
    except ConversionUnavailable:
        raise
    except Exception:
        raise ConversionUnavailable("conversion failed") from None
