"""
утилита для формирования отчетов pdf/html"""

from __future__ import annotations

import html
import io
import math
import os
import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image as RLImage
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_IMAGE_TIMEOUT = (3, 10)
_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_MAX_IMAGE_WIDTH = 170 * mm
_MAX_IMAGE_HEIGHT = 120 * mm
_PDF_FONT_CACHE: tuple[str, str] | None = None
_DEFAULT_CONDITION = "Good Condition"
_CONDITION_TRANSLATION_CACHE: dict[str, str] = {}
_CONDITION_TRANSLATE_TIMEOUT = (2, 10)
_CONDITION_TRANSLATE_MODEL = os.getenv("REPORT_CONDITION_TRANSLATE_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
_CONDITION_TRANSLATE_ENABLED = os.getenv(
    "REPORT_CONDITION_TRANSLATE_ENABLED",
    "1",
).strip().lower() in {"1", "true", "yes", "on"}
_CONDITION_WORD_RE = re.compile(r"[a-zA-Z']+")

_HTML_REPORT_STYLE = """
<style>
  :root {
    --accent: #FFEB04;
    --dark: #121212;
    --muted: #6F6F6F;
    --bg: #ffffff;
    --line: #ECECEC;
    --line-soft: #F4F4F4;
    --soft-bg: #FAFAFA;
    --r-sm: 10px;
    --r-md: 14px;
    --r-lg: 20px;
    --r-pill: 100px;
    --font-display: "Bebas Neue", "Inter", system-ui, sans-serif;
    --font-body: "Inter", system-ui, -apple-system, sans-serif;
  }

  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }

  body {
    font-family: var(--font-body);
    color: var(--dark);
    background: var(--bg);
    font-size: 15px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }

  .page {
    max-width: 1240px;
    margin: 0 auto;
    padding: 28px 20px 60px;
  }

  .header {
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    padding: 18px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 24px;
  }
  .header-main {
    display: flex;
    align-items: center;
    gap: 16px;
    min-width: 0;
  }
  .report-heading { min-width: 0; }
  .report-title {
    font-family: var(--font-display);
    font-size: 28px;
    font-weight: 400;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--dark);
    margin: 0;
    line-height: 1;
  }
  .report-subtitle {
    margin: 4px 0 0;
    font-size: 12px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
    font-weight: 500;
  }
  .generated-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: var(--accent);
    color: var(--dark);
    font-weight: 600;
    font-size: 12px;
    padding: 10px 16px;
    border-radius: var(--r-pill);
    letter-spacing: 0.02em;
    white-space: nowrap;
  }

  .hero {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 36px;
  }
  .hero-left,
  .hero-right {
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    padding: 28px;
    overflow: hidden;
  }
  .hero-left {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .hero-left-text {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .hero-media {
    background: var(--soft-bg);
    border-radius: var(--r-md);
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    aspect-ratio: 4 / 3;
  }
  .hero-image {
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
  }
  .image-fallback {
    color: var(--muted);
    font-size: 14px;
    padding: 22px;
    text-align: center;
  }
  .hero-title {
    font-family: var(--font-display);
    font-size: 36px;
    font-weight: 400;
    line-height: 1.05;
    letter-spacing: 0.01em;
    text-transform: uppercase;
    color: var(--dark);
    margin: 0;
  }
  .hero-condition {
    margin: 0;
    color: var(--muted);
    font-size: 14px;
  }
  .hero-condition strong {
    color: var(--dark);
    font-weight: 600;
  }

  .panel-title {
    font-family: var(--font-display);
    font-size: 28px;
    font-weight: 400;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--dark);
    margin: 0 0 6px;
    line-height: 1.1;
  }
  .panel-subtitle {
    margin: 0 0 20px;
    color: var(--muted);
    font-size: 13px;
  }

  .summary-grid {
    display: flex;
    flex-direction: column;
    gap: 2px;
    background: var(--line);
    border-radius: var(--r-md);
    overflow: hidden;
    margin-bottom: 20px;
  }
  .summary-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    padding: 14px 18px;
    background: var(--bg);
  }
  .summary-key {
    color: var(--muted);
    font-size: 13px;
    font-weight: 500;
  }
  .summary-value {
    color: var(--dark);
    font-size: 15px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    text-align: right;
  }
  .summary-value.good {
    background: var(--accent);
    color: var(--dark);
    padding: 4px 10px;
    border-radius: var(--r-pill);
    font-weight: 700;
  }

  .source-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    background: var(--dark);
    color: var(--bg);
    text-decoration: none;
    font-weight: 600;
    font-size: 13px;
    letter-spacing: 0.02em;
    border-radius: var(--r-pill);
    transition: background 0.18s ease, transform 0.18s ease;
  }
  .source-btn:hover {
    background: #000;
    transform: translateY(-1px);
  }
  .source-btn-lg {
    padding: 14px 24px;
    font-size: 14px;
    width: 100%;
    background: var(--accent);
    color: var(--dark);
  }
  .source-btn-lg:hover { background: #ffe600; }
  .source-btn-sm {
    padding: 10px 18px;
    font-size: 12px;
  }
  .source-btn.is-disabled {
    background: #bdbdbd;
    color: #3a3a3a;
    cursor: not-allowed;
    pointer-events: none;
    transform: none;
  }

  .results { margin-top: 32px; }
  .section-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 20px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--line);
  }
  .section-title {
    font-family: var(--font-display);
    font-size: 36px;
    font-weight: 400;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--dark);
    margin: 0;
    line-height: 1;
  }
  .section-note {
    margin: 0;
    color: var(--muted);
    font-size: 13px;
    font-weight: 500;
  }

  .result-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 20px;
  }
  .result-card {
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: border-color 0.18s ease, transform 0.18s ease, box-shadow 0.18s ease;
  }
  .result-card:hover {
    border-color: var(--dark);
    transform: translateY(-2px);
    box-shadow: 0 8px 24px rgba(0,0,0,0.06);
  }
  .result-media {
    background: var(--soft-bg);
    aspect-ratio: 1 / 1;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .result-image {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .result-body {
    padding: 18px 18px 20px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    flex: 1;
  }
  .result-price-box {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 12px 14px;
    background: var(--soft-bg);
    border-radius: var(--r-sm);
    margin-bottom: 6px;
  }
  .result-price-line {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
  }
  .result-price-label {
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 500;
  }
  .result-price-main {
    font-size: 16px;
    font-weight: 700;
    color: var(--dark);
    font-variant-numeric: tabular-nums;
  }
  .result-price-original {
    font-size: 13px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .result-title {
    font-family: var(--font-body);
    font-size: 14px;
    font-weight: 600;
    line-height: 1.35;
    color: var(--dark);
    margin: 0;
  }
  .result-meta {
    margin: 0;
    font-size: 12px;
    color: var(--muted);
    line-height: 1.5;
  }
  .result-meta strong {
    color: var(--dark);
    font-weight: 600;
  }
  .result-meta-group {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .status-pill {
    display: inline-block;
    padding: 2px 10px;
    border-radius: var(--r-pill);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.02em;
    line-height: 1.4;
    vertical-align: 1px;
  }
  .status-sold {
    background: #FDECEC;
    color: #C53030;
  }
  .status-available {
    background: #E6F6EC;
    color: #1F7A3A;
  }
  .status-no-price {
    background: #FDF1F1;
    color: #A93232;
  }
  .result-meta-sim {
    margin-top: 4px;
    font-size: 12px;
    color: var(--muted);
  }
  .result-card .source-btn {
    margin-top: auto;
    align-self: stretch;
  }

  .empty {
    border: 1px dashed var(--line);
    border-radius: var(--r-lg);
    padding: 24px;
    color: var(--muted);
    text-align: center;
  }
  .note-error {
    margin: 12px 0 0;
    color: #C53030;
    font-size: 13px;
    font-weight: 500;
  }

  @media (max-width: 880px) {
    .page { padding: 20px 16px 40px; }
    .header { padding: 14px 18px; flex-wrap: wrap; gap: 8px; }
    .header-main { flex-direction: column; align-items: flex-start; gap: 6px; }
    .report-subtitle { font-size: 10px; letter-spacing: 0.06em; }
    .hero { grid-template-columns: 1fr; }
    .hero-left, .hero-right { padding: 18px; }
    .hero-left { flex-direction: row; }
    .hero-left-text { display: flex; flex-direction: column; gap: 8px; }
    .hero-media { min-width: 40%; }
    .hero-title { font-size: 28px; }
    .panel-title { font-size: 24px; }
    .section-title { font-size: 28px; }
  }
  @media (max-width: 520px) {
    .report-title { font-size: 22px; }
    .generated-badge { font-size: 11px; padding: 8px 14px; }
    .panel-subtitle { margin: 0 0 12px; }
    .summary-grid { margin-bottom: 12px; }
    .summary-row { padding: 8px 10px; }
    .summary-key { font-size: 12px; }
    .summary-value { font-size: 13px; }
    .hero-title { font-size: 22px; }
    .section-title { font-size: 24px; }
    .result-body { padding: 14px; gap: 8px; }
    .result-grid {
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    .result-price-box { padding: 10px 12px; }
    .result-price-main { font-size: 14px; }
    .result-price-original { font-size: 12px; }
    .result-price-label { font-size: 10px; }
    .result-title { font-size: 13px; }
    .result-meta,
    .result-meta-sim { font-size: 11px; line-height: 1.4; }
    .source-btn-sm { padding: 9px 14px; font-size: 11px; }
  }
  @media (max-width: 460px) {
    .result-body { padding: 8px; gap: 6px; }
    .result-price-box { padding: 8px; }
  }
  @media (max-width: 360px) {
    .result-grid { grid-template-columns: 1fr; }
  }
</style>
"""


def get_results_dir(project_root: str) -> str:
    # возвращает папку для результатов и при необходимости создает ее
    results_dir = os.path.join(project_root, "results")
    os.makedirs(results_dir, exist_ok=True)
    return results_dir


def build_results_timestamp(now: datetime | None = None) -> str:
    # формирует единый timestamp для связанных артефактов
    current = now or datetime.now()
    return current.strftime("%Y-%m-%d_%H-%M-%S")


def save_pdf_report(
    payload: dict,
    results_dir: str,
    timestamp: str,
    generated_at: datetime | None = None,
) -> tuple[str, str]:
    # сохраняет pdf-отчет на диск и возвращает путь и имя файла
    filename = f"report_{timestamp}.pdf"
    path = os.path.join(results_dir, filename)
    build_pdf_report(payload, output_path=path, generated_at=generated_at)
    return path, filename


def save_html_report(
    payload: dict,
    results_dir: str,
    timestamp: str,
    generated_at: datetime | None = None,
) -> tuple[str, str]:
    # сохраняет html-отчет на диск и возвращает путь и имя файла
    filename = f"report_{timestamp}.html"
    path = os.path.join(results_dir, filename)
    build_html_report(payload, output_path=path, generated_at=generated_at)
    return path, filename


def build_html_report(payload: dict, output_path: str, generated_at: datetime | None = None) -> None:
    # собирает и записывает html-отчет по итогам анализа
    created_at = generated_at or datetime.now()
    created_str = created_at.strftime("%Y-%m-%d %H:%M:%S")
    html_report = _build_html_markup(payload or {}, created_str)
    with open(output_path, "w", encoding="utf-8") as file_obj:
        file_obj.write(html_report)


def build_pdf_report(payload: dict, output_path: str, generated_at: datetime | None = None) -> None:
    # собирает и записывает pdf-отчет по итогам анализа
    created_at = generated_at or datetime.now()
    created_str = created_at.strftime("%Y-%m-%d %H:%M:%S")
    font_regular, font_bold = _get_pdf_fonts()

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title="bag prices report",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "report_title",
        parent=styles["Title"],
        fontName=font_bold,
        fontSize=18,
        leading=22,
        spaceAfter=10,
    )
    section_style = ParagraphStyle(
        "report_section",
        parent=styles["Heading2"],
        fontName=font_bold,
        fontSize=13,
        leading=16,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "report_body",
        parent=styles["BodyText"],
        fontName=font_regular,
        fontSize=10,
        leading=13,
        spaceAfter=3,
    )
    muted_style = ParagraphStyle(
        "report_muted",
        parent=styles["BodyText"],
        fontName=font_regular,
        textColor=colors.HexColor("#667085"),
        fontSize=9,
        leading=12,
        spaceAfter=4,
    )

    story = []
    story.append(Paragraph("report bag prices", title_style))
    story.append(Paragraph(f"сформировано: {_safe_text(created_str)}", body_style))
    story.append(Paragraph(f"ai target: {_safe_text(payload.get('ai_target_name') or 'N/A')}", body_style))
    story.append(Spacer(1, 4))

    avito_line = _build_avito_line(payload)
    story.append(Paragraph(avito_line, body_style))

    summary_rows = [
        ["медиана итог", _format_money(payload.get("median_price_usd"), "USD")],
        ["медиана в наличии", _format_money(payload.get("median_price_available_usd"), "USD")],
        ["медиана продано", _format_money(payload.get("median_price_sold_usd"), "USD")],
        ["медиана все", _format_money(payload.get("median_price_raw_usd"), "USD")],
        ["карточек", str(len(payload.get("items") or []))],
    ]
    summary_table = Table(summary_rows, colWidths=[70 * mm, 95 * mm])
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f5f7fb")),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#172236")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dbe2ef")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#dbe2ef")),
                ("FONTNAME", (0, 0), (0, -1), font_bold),
                ("FONTNAME", (1, 0), (1, -1), font_regular),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(summary_table)

    items = payload.get("items") or []
    if not items:
        story.append(Spacer(1, 12))
        story.append(Paragraph("по запросу не найдено карточек для отчета", muted_style))
        doc.build(story)
        return

    for index, item in enumerate(items, start=1):
        story.append(PageBreak())
        story.append(Paragraph(f"карточка {index}", section_style))
        title = _safe_text(item.get("title") or "без названия")
        story.append(Paragraph(title, body_style))
        story.append(Spacer(1, 4))

        details_rows = [
            ["сайт", _safe_text(item.get("site") or "N/A")],
            ["статус", _safe_text(item.get("status") or "N/A")],
            ["цена usd", _format_money(item.get("price"), item.get("currency") or "USD")],
            ["оригинальная цена", _format_money(item.get("price_original"), item.get("currency_original"))],
            ["схожесть", _format_similarity(item.get("similarity"))],
            ["ссылка", _safe_text(item.get("url") or "N/A")],
        ]
        details_table = Table(details_rows, colWidths=[45 * mm, 120 * mm])
        details_table.setStyle(
            TableStyle(
                [
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dbe2ef")),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#dbe2ef")),
                    ("FONTNAME", (0, 0), (0, -1), font_bold),
                    ("FONTNAME", (1, 0), (1, -1), font_regular),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("WORDWRAP", (1, 0), (1, -1), "CJK"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(details_table)
        story.append(Spacer(1, 8))

        image_url = (item.get("image") or "").strip()
        image_block = _build_image_block(image_url)
        if image_block is not None:
            story.append(image_block)
        else:
            story.append(Paragraph("изображение недоступно", muted_style))

    doc.build(story)


def _build_avito_line(payload: dict) -> str:
    # формирует строку по цене avito
    avito_price_original = payload.get("avito_price_original")
    avito_currency_original = payload.get("avito_currency_original")
    avito_price_usd = payload.get("avito_price_usd")
    avito_price_error = payload.get("avito_price_error")

    if avito_price_original is None:
        return "avito price was not provided"

    avito_original_text = _format_money(avito_price_original, avito_currency_original)
    avito_usd_text = _format_money(avito_price_usd, "USD") if avito_price_usd is not None else "N/A"
    if avito_price_error:
        return (
            f"avito price: {_safe_text(avito_original_text)} | "
            f"usd: {_safe_text(avito_usd_text)} | {_safe_text(avito_price_error)}"
        )
    return f"avito price: {_safe_text(avito_original_text)} | usd: {_safe_text(avito_usd_text)}"


def _build_image_block(image_url: str) -> RLImage | None:
    # скачивает и подготавливает изображение для вставки в pdf
    image_bytes = _fetch_image_bytes(image_url)
    if not image_bytes:
        return None

    try:
        image_stream = io.BytesIO(image_bytes)
        reader = ImageReader(image_stream)
        width, height = reader.getSize()
        if not width or not height:
            return None

        scale = min(_MAX_IMAGE_WIDTH / float(width), _MAX_IMAGE_HEIGHT / float(height), 1.0)
        image_stream.seek(0)
        image = RLImage(image_stream)
        image.drawWidth = float(width) * scale
        image.drawHeight = float(height) * scale
        return image
    except Exception:
        return None


def _fetch_image_bytes(image_url: str) -> bytes | None:
    # загружает изображение по url с ограничением размера
    if not image_url:
        return None

    try:
        response = requests.get(
            image_url,
            timeout=_IMAGE_TIMEOUT,
            stream=True,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*,*/*;q=0.8"},
        )
        response.raise_for_status()

        total = 0
        chunks = []
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            total += len(chunk)
            if total > _IMAGE_MAX_BYTES:
                return None
            chunks.append(chunk)

        if not chunks:
            return None
        return b"".join(chunks)
    except Exception:
        return None


def _format_money(value, currency: str | None) -> str:
    # форматирует цену для отчета
    if value is None:
        return "N/A"

    try:
        amount = float(value)
    except Exception:
        return "N/A"

    amount = math.trunc(amount)
    currency_text = str(currency).strip() if currency else ""
    if currency_text:
        return f"{amount} {currency_text}"
    return f"{amount}"


def _format_similarity(value) -> str:
    # форматирует схожесть в проценты
    if value is None:
        return "N/A"
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return "N/A"


def _safe_text(value) -> str:
    # экранирует текст для безопасной вставки
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def _safe_attr(value) -> str:
    # экранирует текст для вставки в html-атрибут
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _safe_url(value) -> str | None:
    # проверяет и возвращает безопасный http/https url
    text = str(value or "").strip()
    if not text:
        return None

    try:
        parsed = urlparse(text)
    except Exception:
        return None

    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    return text


def _resolve_condition(item: dict | None) -> str:
    # возвращает condition для отчета с дефолтным значением
    if not isinstance(item, dict):
        return _DEFAULT_CONDITION

    raw_value = str(item.get("condition") or "").strip()
    if not raw_value:
        return _DEFAULT_CONDITION

    lowered = raw_value.lower()
    if lowered in {"unknown", "n/a", "none", "null", "неизвестно"}:
        return _DEFAULT_CONDITION
    return raw_value


def _condition_looks_english(value: str) -> bool:
    # определяет похоже ли condition на английский
    if not value:
        return True

    if re.search(r"[^\x00-\x7F]", value):
        return False

    tokens = [token.lower() for token in _CONDITION_WORD_RE.findall(value)]
    if not tokens:
        return True

    known_words = {
        "new",
        "brand",
        "excellent",
        "very",
        "good",
        "fair",
        "poor",
        "used",
        "pre",
        "owned",
        "preowned",
        "condition",
        "vintage",
        "mint",
        "like",
        "wear",
        "shows",
        "showing",
        "with",
        "without",
        "light",
        "minor",
        "visible",
        "signs",
        "sign",
        "of",
        "scratches",
        "scratch",
        "marks",
        "mark",
        "stains",
        "stain",
        "patina",
        "restored",
        "rare",
        "authentic",
        "unused",
        "pristine",
        "as",
        "is",
        "a",
        "an",
        "the",
        "well",
        "maintained",
        "flaws",
        "flaw",
        "outside",
        "inside",
    }
    return all(token in known_words for token in tokens)


def _translate_condition_with_openai(value: str) -> str:
    # переводит condition на английский через openai api
    if not value:
        return value
    if value in _CONDITION_TRANSLATION_CACHE:
        return _CONDITION_TRANSLATION_CACHE[value]
    if _condition_looks_english(value):
        _CONDITION_TRANSLATION_CACHE[value] = value
        return value
    if not _CONDITION_TRANSLATE_ENABLED:
        _CONDITION_TRANSLATION_CACHE[value] = value
        return value

    api_key = (
        os.getenv("OPENAI_API_KEY")
        or os.getenv("LOCAL_TITLE_AI_OPENAI_API_KEY")
        or os.getenv("CHATGPT_API_KEY")
        or ""
    ).strip()
    if not api_key:
        _CONDITION_TRANSLATION_CACHE[value] = value
        return value

    base_url = (os.getenv(
        "OPENAI_BASE_URL",
        "https://api.openai.com/v1",
    ) or "https://api.openai.com/v1").strip().rstrip("/")
    endpoint = f"{base_url}/chat/completions"
    payload = {
        "model": _CONDITION_TRANSLATE_MODEL,
        "temperature": 0,
        "max_tokens": 32,
        "messages": [
            {
                "role": "system",
                "content": (
                    "you translate short product condition labels to english "
                    "return only concise english text"
                ),
            },
            {
                "role": "user",
                "content": (
                    "translate to english if needed "
                    "if already english return as is "
                    f"condition: {value}"
                ),
            },
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    translated = value
    try:
        response = requests.post(
            endpoint,
            json=payload,
            headers=headers,
            timeout=_CONDITION_TRANSLATE_TIMEOUT,
        )
        if response.status_code == 200:
            data = response.json()
            choices = data.get("choices") if isinstance(data, dict) else None
            message = choices[0].get("message") if isinstance(choices, list) and choices else {}
            content = str((message or {}).get("content") or "").strip()
            if content:
                translated = content.splitlines()[0].strip().strip('"').strip("'") or value
    except Exception:
        translated = value

    _CONDITION_TRANSLATION_CACHE[value] = translated
    return translated


def _build_translated_conditions_map(items: list[dict]) -> dict[str, str]:
    # строит карту condition с переводом на английский
    mapping: dict[str, str] = {}
    for item in items:
        condition = _resolve_condition(item)
        if condition in mapping:
            continue
        mapping[condition] = _translate_condition_with_openai(condition)
    return mapping


def _resolve_condition_translated(item: dict | None, translated_map: dict[str, str] | None) -> str:
    # возвращает condition с учетом карты переводов
    condition = _resolve_condition(item)
    if not translated_map:
        return condition
    return translated_map.get(condition, condition)


def _build_source_button(url: str | None, *, large: bool) -> str:
    # формирует кнопку перехода на источник
    size_class = "source-btn-lg" if large else "source-btn-sm"
    if url:
        label = "Go to source page" if large else "Open source"
        return (
            f'<a class="source-btn {size_class}" href="{_safe_attr(url)}" '
            'target="_blank" rel="noopener noreferrer">'
            f"{_safe_text(label)}</a>"
        )
    return f'<button class="source-btn {size_class} is-disabled" type="button" disabled>Source is unavailable</button>'


def _build_html_image(url: str | None, css_class: str, alt_text: str) -> str:
    # формирует html-блок изображения
    if not url:
        return '<div class="image-fallback">Image unavailable</div>'
    return (
        f'<img class="{_safe_attr(css_class)}" src="{_safe_attr(url)}" '
        f'alt="{_safe_attr(alt_text)}" loading="lazy" decoding="async">'
    )


def _build_status_badge(item: dict | None) -> tuple[str, str]:
    # возвращает текст статуса и css-класс для цветовой индикации
    if not isinstance(item, dict):
        return "No price", "status-no-price"

    raw_status = str(item.get("status") or "").strip()
    normalized = raw_status.lower()

    sold_markers = {
        "sold",
        "sold out",
        "out of stock",
        "продан",
        "продано",
        "нет в наличии",
    }
    has_price = item.get("price") is not None

    if not has_price:
        return "No price", "status-no-price"

    if any(marker in normalized for marker in sold_markers if marker):
        return "Sold", "status-sold"

    return "Available", "status-available"


def _build_items_html(items: list[dict], translated_conditions: dict[str, str] | None = None) -> str:
    # формирует html списка найденных карточек
    if not items:
        return '<div class="empty">No cards found for this report</div>'

    html_blocks = []
    for index, item in enumerate(items, start=1):
        title = _safe_text(item.get("title") or "Untitled")
        site = _safe_text(item.get("site") or "N/A")
        status_text, status_class = _build_status_badge(item)
        status = _safe_text(status_text)
        condition = _safe_text(_resolve_condition_translated(item, translated_conditions))
        similarity = _safe_text(_format_similarity(item.get("similarity")))
        price_usd = _safe_text(_format_money(item.get("price"), item.get("currency") or "USD"))
        price_original = _safe_text(_format_money(item.get("price_original"), item.get("currency_original")))
        source_url = _safe_url(item.get("url"))
        image_url = _safe_url(item.get("image"))
        image_html = _build_html_image(image_url, "result-image", f"item image {index}")
        source_button = _build_source_button(source_url, large=False)
        price_box = (
            '<div class="result-price-box">'
            '<div class="result-price-line">'
            '<span class="result-price-label">Price USD</span>'
            f'<span class="result-price-main">{price_usd}</span>'
            "</div>"
            '<div class="result-price-line">'
            '<span class="result-price-label">Original price</span>'
            f'<span class="result-price-original">{price_original}</span>'
            "</div>"
            "</div>"
        )

        html_blocks.append(
            (
                '<article class="result-card">'
                f'<div class="result-media">{image_html}</div>'
                '<div class="result-body">'
                f"{price_box}"
                f'<h3 class="result-title">{index}. {title}</h3>'
                '<div class="result-meta-group">'
                f'<p class="result-meta result-meta-primary"><strong>Condition:</strong> {condition}</p>'
                f'<p class="result-meta result-meta-primary"><strong>Site:</strong> {site}</p>'
                f'<p class="result-meta result-meta-primary"><strong>Status:</strong> <span class="status-pill {_safe_attr(status_class)}">{status}</span></p>'
                f'<p class="result-meta result-meta-sim"><strong>Similarity:</strong> {similarity}</p>'
                "</div>"
                f"{source_button}"
                "</div>"
                "</article>"
            )
        )

    return "".join(html_blocks)


def _build_html_markup(payload: dict, created_str: str) -> str:
    # собирает html-разметку отчета
    items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
    primary_item = items[0] if items else {}
    translated_conditions = _build_translated_conditions_map(items)
    primary_title = _safe_text(primary_item.get("title") or payload.get("ai_target_name") or "Untitled")
    primary_condition = _safe_text(_resolve_condition_translated(primary_item, translated_conditions))
    primary_image_url = _safe_url(primary_item.get("image"))
    primary_source_url = _safe_url(primary_item.get("url"))

    avito_original_text = (
        _format_money(payload.get("avito_price_original"), payload.get("avito_currency_original"))
        if payload.get("avito_price_original") is not None
        else "N/A"
    )
    avito_usd_text = (
        _format_money(payload.get("avito_price_usd"), "USD")
        if payload.get("avito_price_usd") is not None
        else "N/A"
    )
    avito_error = _safe_text(payload.get("avito_price_error") or "")

    summary_rows = [
        ("Avito original price", avito_original_text, "summary-value"),
        ("Avito price in USD", avito_usd_text, "summary-value"),
        ("Final median", _format_money(payload.get("median_price_usd"), "USD"), "summary-value good"),
        ("Available median", _format_money(payload.get("median_price_available_usd"), "USD"), "summary-value"),
        ("Sold median", _format_money(payload.get("median_price_sold_usd"), "USD"), "summary-value"),
        ("All median", _format_money(payload.get("median_price_raw_usd"), "USD"), "summary-value"),
        ("Cards total", str(len(items)), "summary-value"),
    ]
    summary_rows_html = "".join(
        (
            '<div class="summary-row">'
            f'<span class="summary-key">{_safe_text(label)}</span>'
            f'<span class="{_safe_attr(value_class)}">{_safe_text(value_text)}</span>'
            "</div>"
        )
        for label, value_text, value_class in summary_rows
    )

    primary_image_html = _build_html_image(primary_image_url, "hero-image", "main image")
    source_button_html = _build_source_button(primary_source_url, large=True)
    items_html = _build_items_html(items, translated_conditions=translated_conditions)
    target_name = _safe_text(payload.get("ai_target_name") or "N/A")
    avito_error_html = f'<p class="note-error">Avito conversion issue: {avito_error}</p>' if avito_error else ""
    return (
        "<!doctype html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Bag Price Analysis Report</title>"
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Inter:ital,opsz,wght@0,14..32,100..900;1,14..32,100..900&display=swap" rel="stylesheet">'
        f"{_HTML_REPORT_STYLE}"
        "</head>"
        "<body>"
        '<div class="page">'
        '<header class="header">'
        '<div class="header-main">'
        '<div class="report-heading">'
        '<h1 class="report-title">Bag Price Analysis Report</h1>'
        f'<p class="report-subtitle">AI Target: {target_name}</p>'
        "</div>"
        "</div>"
        f'<div class="generated-badge">Generated: {_safe_text(created_str)}</div>'
        "</header>"
        '<section class="hero">'
        '<article class="hero-left">'
        f'<div class="hero-media">{primary_image_html}</div>'
        '<div class="hero-left-text">'
        f'<h2 class="hero-title">{primary_title}</h2>'
        f'<p class="hero-condition"><strong>Condition:</strong> {primary_condition}</p>'
        "</div>"
        "</article>"
        '<article class="hero-right">'
        '<h3 class="panel-title">Price Summary</h3>'
        '<p class="panel-subtitle">Avito pricing and market medians</p>'
        f'<div class="summary-grid">{summary_rows_html}</div>'
        f"{source_button_html}"
        f"{avito_error_html}"
        "</article>"
        "</section>"
        '<section class="results">'
        '<div class="section-head">'
        '<h2 class="section-title">Cards in Report</h2>'
        f'<p class="section-note">Total: {_safe_text(str(len(items)))}</p>'
        "</div>"
        f'<div class="result-grid">{items_html}</div>'
        "</section>"
        "</div>"
        "</body>"
        "</html>"
    )


def _get_pdf_fonts() -> tuple[str, str]:
    # выбирает и кэширует шрифты с поддержкой кириллицы
    global _PDF_FONT_CACHE
    if _PDF_FONT_CACHE is not None:
        return _PDF_FONT_CACHE

    candidates = [
        (
            "DejaVuSans",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
        (
            "ArialUnicodeMS",
            "/mnt/c/Windows/Fonts/arial.ttf",
            "/mnt/c/Windows/Fonts/arialbd.ttf",
        ),
    ]

    for base_name, regular_path, bold_path in candidates:
        if not os.path.exists(regular_path):
            continue

        regular_name = f"{base_name}-Regular"
        bold_name = f"{base_name}-Bold"
        try:
            if regular_name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(regular_name, regular_path))

            if os.path.exists(bold_path):
                if bold_name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(bold_name, bold_path))
            else:
                bold_name = regular_name

            _PDF_FONT_CACHE = (regular_name, bold_name)
            return _PDF_FONT_CACHE
        except Exception:
            continue

    _PDF_FONT_CACHE = ("Helvetica", "Helvetica-Bold")
    return _PDF_FONT_CACHE
