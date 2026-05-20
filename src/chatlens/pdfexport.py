"""Render an Asset to a self-contained PDF (text + inline images, CJK + emoji).

Engine: fpdf2 — pure Python, so it bundles inside the .app with no system
libraries. Fonts:
  - Body / CJK: Arial Unicode MS, a glyf-based TrueType that ships with macOS.
    fpdf2 embeds it as a CID TrueType / Identity-H font, which renders correctly
    in both macOS Quartz (Preview) and pdf.js (Obsidian).
  - Emoji: Noto Emoji (monochrome), bundled under fonts/, registered as an
    fpdf2 fallback so emoji that Arial Unicode MS lacks still render (B&W).

No WeasyPrint (needs the pango system library) and no Ghostscript — a freshly
downloaded .app works with zero extra installs. Backs POST /api/assets/{id}/export;
heavy lifting stays out of the router, like chatlens.llm / chatlens.wechat.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

_KIND_LABEL = {
    "daily_summary": "群日报",
    "daily_digest": "每日精华",
    "guest_share": "嘉宾分享",
}

# Arial Unicode MS ships with macOS and covers CJK + most of Unicode.
_CJK_FONT_PATHS = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
)
# Monochrome Noto Emoji, bundled — fpdf2 fallback for emoji glyphs.
_EMOJI_FONT = Path(__file__).resolve().parent / "fonts" / "NotoEmoji-Regular.ttf"

_MEDIA_URL = "/api/media/"
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_IMG = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_IMG_TAG = re.compile(r"\[图片\s+([^\]\s]+)")  # wechat-cli transcript image tag
_ORDERED = re.compile(r"^(\d+)\.\s+(.*)$")
# In-app links use #anchors — [text](#asset/...), [text](#topic/...). fpdf2's
# markdown treats a #anchor as a named destination it never sets (crashing the
# export). Drop just the '#' so they survive as accent-coloured links (pointing
# at a harmless dead relative URI) instead of crashing or losing their colour.
# Real http(s) links are left untouched.
_HASH_LINK = re.compile(r"(?<!!)(\[[^\]]+\]\()#")
# Markdown links, and the emoji plane. Emoji inside a link fragment miss
# fpdf2's fallback font (rendering as tofu), so they're stripped from link
# text only — see _clean(). Non-raw string so \U escapes decode to the chars.
_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]*)\)")
_EMOJI = re.compile("[\U0001f000-\U0001faff]")
_INK = (0x37, 0x41, 0x51)
_HEAD = (0x0F, 0x17, 0x2A)

# Zero-width emoji selectors / joiners (variation selectors, ZWJ, the keycap
# combining mark). fpdf2 does no complex-text shaping, so left in they render
# as tofu boxes; deleting them leaves the base characters and any standalone
# emoji rendering cleanly. Mapped to None so str.translate() drops them.
_EMOJI_SELECTORS = {0xFE0F: None, 0xFE0E: None, 0x200D: None, 0x20E3: None}


def _clean(text: str) -> str:
    text = text.translate(_EMOJI_SELECTORS)
    text = _HASH_LINK.sub(r"\1", text)
    # Emoji inside a markdown-link fragment render as tofu (no fallback there),
    # so drop them from link text only; plain text keeps its emoji.
    return _LINK.sub(lambda m: f"[{_EMOJI.sub('', m[1])}]({m[2]})", text)


class PDFExportUnavailable(RuntimeError):
    """Raised when fpdf2 or the macOS CJK font isn't available."""


def _px_to_mm(px: float) -> float:
    return px * 25.4 / 96.0


def _place_image(pdf, url: str, media_root: str) -> None:
    """Draw one inline image, scaled to fit the page; skip remote/missing ones."""
    path = f"{media_root}/{url[len(_MEDIA_URL):]}" if url.startswith(_MEDIA_URL) else url
    if "://" in path or not Path(path).is_file():
        return  # remote / data: / missing — skip quietly
    try:
        from PIL import Image
        with Image.open(path) as im:
            iw, ih = im.size
        if not iw or not ih:
            return
        w = min(pdf.epw, _px_to_mm(iw))
        h = w * ih / iw
        max_h = pdf.h - pdf.t_margin - pdf.b_margin
        if h > max_h:                       # never taller than a single page
            h, w = max_h, max_h * iw / ih
        if pdf.get_y() + h > pdf.h - pdf.b_margin:
            pdf.add_page()
        pdf.image(path, x=pdf.l_margin, w=w, h=h)
        pdf.ln(3)
    except Exception as e:  # noqa: BLE001 — a bad image must not fail the export
        log.warning("could not embed image %s: %s", path, e)


def _block(pdf, text: str, size: float, lh: float, *, bold: bool = False,
           color=_INK, indent: float = 0.0, fill: bool = False, gap: float = 1.5) -> None:
    """Render one text block. `markdown=True` handles inline **bold** and links."""
    pdf.set_font("CJK", "B" if bold else "", size)
    pdf.set_text_color(*color)
    if indent:
        pdf.set_x(pdf.l_margin + indent)
    # align="L": fpdf2's multi_cell defaults to justified, which stretches the
    # sparse spaces in CJK lines into ragged gaps.
    pdf.multi_cell(0, lh, _clean(text), new_x="LMARGIN", new_y="NEXT",
                   markdown=True, fill=fill, align="L")
    if gap:
        pdf.ln(gap)


def _render_markdown(pdf, body_md: str) -> None:
    """Walk the asset's markdown line by line — a port of the SPA's renderMd."""
    media_root = str((settings.data_dir / "media").resolve())
    for raw in (body_md or "").split("\n"):
        line = raw.rstrip()
        # An image, optionally as a list item — drawn, not printed as text.
        body = line[2:] if line[:2] in ("- ", "* ") else line
        img = _IMG.search(body)
        if img and not _IMG.sub("", body).strip():
            _place_image(pdf, img.group(1), media_root)
        elif not line.strip():
            pdf.ln(2)
        elif line.startswith("# "):
            _block(pdf, line[2:], 17, 9, bold=True, color=_HEAD, gap=3)
        elif line.startswith("## "):
            pdf.ln(2)
            _block(pdf, line[3:], 13.5, 7.5, bold=True, color=_HEAD, gap=2)
        elif line.startswith("### "):
            _block(pdf, line[4:], 11.5, 6.5, bold=True, color=_HEAD, gap=1.5)
        elif line.startswith("> "):
            pdf.set_fill_color(0xEE, 0xF2, 0xFF)
            _block(pdf, line[2:], 10.5, 6, color=(0x43, 0x38, 0xCA), fill=True, gap=2)
        elif line[:2] in ("- ", "* "):
            _block(pdf, f"•  {line[2:]}", 11, 6, indent=3)
        elif _ORDERED.match(line):
            m = _ORDERED.match(line)
            _block(pdf, f"{m.group(1)}.  {m.group(2)}", 11, 6, indent=3)
        else:
            _block(pdf, line, 11, 6)


def _render_original_chat(pdf, asset) -> None:
    """Append the 原始对话 thread the app shows below a 嘉宾分享. Its source
    messages live in meta_json (topics + messages), not in body_md."""
    if getattr(asset, "kind", "") != "guest_share":
        return
    try:
        meta = json.loads(asset.meta_json or "")
    except (json.JSONDecodeError, TypeError):
        return
    messages = meta.get("messages") or {}
    lines: list = []
    for topic in meta.get("topics") or []:
        lines.extend(topic.get("lines") or [])
    msgs = [m for m in (messages.get(str(ln)) for ln in lines) if m]
    if not msgs:
        return

    media_root = str((settings.data_dir / "media").resolve())
    pdf.ln(3)
    pdf.set_draw_color(0xE5, 0xE7, 0xEB)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(3)
    _block(pdf, f"📜 原始对话 · {len(msgs)} 条", 13.5, 7.5, bold=True, color=_HEAD, gap=3)
    for m in msgs:
        head = "  ".join(p for p in (m.get("sender", ""), m.get("ts", "")) if p)
        if head:
            _block(pdf, head, 8.5, 4.6, color=(0x94, 0xA3, 0xB8), gap=0.2)
        text = (m.get("text") or "").strip()
        tag = _IMG_TAG.search(text)
        if tag:
            _place_image(pdf, f"{_MEDIA_URL}{asset.id}/{tag.group(1)}", media_root)
        elif text:
            _block(pdf, text, 10, 5.5, gap=2)
        else:
            pdf.ln(1.5)


def render_asset_pdf(asset, group_name: str) -> bytes:
    """Return `asset` rendered as PDF bytes (text + inline images, CJK + emoji)."""
    try:
        from fpdf import FPDF
    except ImportError as e:
        raise PDFExportUnavailable("PDF 导出需要 fpdf2。请运行： pip install fpdf2") from e

    cjk = next((p for p in _CJK_FONT_PATHS if Path(p).is_file()), None)
    if not cjk:
        raise PDFExportUnavailable(
            "找不到 Arial Unicode MS 字体（macOS 自带，"
            "通常位于 /System/Library/Fonts/Supplemental/）。"
        )

    pdf = FPDF()
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(auto=True, margin=16)
    # Markdown links in the app's accent blue, no underline — fpdf2 leaves
    # them uncoloured (MARKDOWN_LINK_COLOR defaults to None) otherwise.
    pdf.MARKDOWN_LINK_COLOR = (0x4F, 0x46, 0xE5)
    pdf.MARKDOWN_LINK_UNDERLINE = False
    # One .ttf registered for every style — Arial Unicode MS has no separate
    # bold/italic cut, so bold falls back to regular weight (size carries it).
    # The emoji font must also cover every style: registered for "" only, the
    # fallback finds no match in a bold run and emoji silently vanish.
    for style in ("", "B", "I"):
        pdf.add_font("CJK", style, cjk)
    if _EMOJI_FONT.is_file():
        for style in ("", "B", "I"):
            pdf.add_font("Emoji", style, str(_EMOJI_FONT))
        pdf.set_fallback_fonts(["Emoji"])
    pdf.add_page()

    meta = " · ".join(
        str(b) for b in
        (_KIND_LABEL.get(asset.kind, asset.kind), group_name, asset.for_date) if b
    )
    pdf.set_font("CJK", "", 9)
    pdf.set_text_color(0x64, 0x74, 0x8B)
    pdf.multi_cell(0, 6, _clean(meta), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.ln(3)

    _render_markdown(pdf, asset.body_md)
    _render_original_chat(pdf, asset)
    return bytes(pdf.output())


def pdf_filename(asset) -> str:
    """A filesystem-safe `.pdf` name built from the asset title + date."""
    stem = (asset.title or "精华").strip()
    if asset.for_date and asset.for_date not in stem:
        stem = f"{stem} · {asset.for_date}"
    stem = _UNSAFE.sub(" ", stem).strip() or "chatlens-export"
    return f"{stem}.pdf"


def export_asset_to_downloads(asset, group_name: str) -> Path:
    """Render `asset` to a PDF written straight into ~/Downloads — no browser
    download and no save dialog, so it behaves identically in a browser and in
    the bundled .app. A numeric suffix is added if the name already exists.
    """
    pdf_bytes = render_asset_pdf(asset, group_name)
    downloads = Path.home() / "Downloads"
    target_dir = downloads if downloads.is_dir() else settings.data_dir
    name = Path(pdf_filename(asset))
    dest = target_dir / name.name
    n = 2
    while dest.exists():
        dest = target_dir / f"{name.stem} ({n}){name.suffix}"
        n += 1
    dest.write_bytes(pdf_bytes)
    log.info("exported asset %s -> %s", asset.id, dest)
    return dest
