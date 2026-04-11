from __future__ import annotations

import json
import os
import sqlite3
from html import escape as html_escape
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_file, send_from_directory

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as reportlab_canvas
    from reportlab.platypus import (
        Image as RLImage,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "packing_list.db"
HTML_FILE = "packing_list_creator.html"

app = Flask(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_qty(value: Any) -> str:
    num = round(to_float(value, 0.0) + 1e-9, 2)
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:.2f}"


def summarize_delivery_items(rows_raw: Any) -> list[dict[str, Any]]:
    rows = rows_raw if isinstance(rows_raw, list) else []
    summary: dict[str, dict[str, Any]] = {}

    for raw in rows:
        row = raw if isinstance(raw, dict) else {}
        description = normalize_text(row.get("description"))
        if not description:
            continue

        pkg_from = format_package_value(row.get("pkg_from"))
        pkg_to = format_package_value(row.get("pkg_to"))
        if not pkg_from and pkg_to:
            pkg_from = pkg_to
        if not pkg_to and pkg_from:
            pkg_to = pkg_from

        qty_per_crt = max(0.0, to_float(row.get("qty_per_crt"), 0.0))
        no_crts = package_count(pkg_from, pkg_to) if pkg_from and pkg_to else 0
        line_total = qty_per_crt * no_crts

        key = description.lower()
        if key not in summary:
            summary[key] = {"description": description, "total_qty": 0.0}
        summary[key]["total_qty"] += line_total

    return list(summary.values())


def normalize_multiline_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def format_package_value(raw: Any) -> str:
    try:
        num = int(str(raw or "").strip())
    except (TypeError, ValueError):
        return ""
    if num < 0:
        return ""
    return f"{num:02d}"


def package_count(from_val: str, to_val: str) -> int:
    try:
        start = int(from_val)
        end = int(to_val)
    except (TypeError, ValueError):
        return 0
    if end < start:
        return 0
    return (end - start) + 1


def package_range_numbers(from_val: str, to_val: str) -> list[int]:
    try:
        start = int(from_val)
        end = int(to_val)
    except (TypeError, ValueError):
        return []
    if end < start:
        return []
    return list(range(start, end + 1))


def package_print_text(from_val: str, to_val: str) -> str:
    if not from_val and not to_val:
        return ""
    if from_val and not to_val:
        to_val = from_val
    if to_val and not from_val:
        from_val = to_val
    if from_val == to_val:
        return from_val
    return f"{from_val} to {to_val}"


def maybe_add_kg(value: str) -> str:
    txt = normalize_text(value)
    if not txt:
        return ""
    if "kg" in txt.lower():
        return txt
    return f"{txt} kg"


def para_from_multiline(text: str, style: Any) -> Any:
    lines = normalize_multiline_text(text).split("\n")
    safe_lines = []
    for line in lines:
        safe = html_escape(line.strip()) if line.strip() else "&nbsp;"
        safe_lines.append(safe)
    html = "<br/>".join(safe_lines) if safe_lines else "&nbsp;"
    return Paragraph(html, style)


def para_from_text(text: str, style: Any) -> Any:
    return Paragraph(html_escape(normalize_text(text) or " "), style)


if REPORTLAB_AVAILABLE:
    class NumberedCanvas(reportlab_canvas.Canvas):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._saved_page_states: list[dict[str, Any]] = []

        def showPage(self) -> None:
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            total_pages = len(self._saved_page_states)
            for state in self._saved_page_states:
                self.__dict__.update(state)
                self.draw_page_number(total_pages)
                super().showPage()
            super().save()

        def draw_page_number(self, total_pages: int) -> None:
            if total_pages <= 0:
                return
            page_width, _ = A4
            self.setFont("Helvetica", 9)
            self.setFillColor(colors.black)
            self.drawRightString(page_width - (10 * mm), 10 * mm, f"PAGE {self._pageNumber} OF {total_pages}")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS packing_lists (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                data_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                billing_address TEXT NOT NULL DEFAULT '',
                shipping_address TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS item_descriptions (
                description TEXT PRIMARY KEY COLLATE NOCASE,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def build_packing_pdf_bytes(payload: dict[str, Any]) -> bytes:
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("PDF engine unavailable on server.")

    data = payload if isinstance(payload, dict) else {}
    inv_ref = normalize_text(data.get("inv_ref")) or "packing-list"
    packing_date = normalize_text(data.get("packing_date"))
    gross_weight = maybe_add_kg(normalize_text(data.get("gross_weight")))
    no_pallets_raw = normalize_text(data.get("no_pallets"))
    remarks = normalize_multiline_text(data.get("remarks"))
    notes = normalize_multiline_text(data.get("notes"))
    billing_address = normalize_multiline_text(data.get("billing_address"))
    shipping_address = normalize_multiline_text(data.get("shipping_address"))

    rows_raw = data.get("rows") if isinstance(data.get("rows"), list) else []
    normalized_rows: list[dict[str, Any]] = []
    unique_packages: set[str] = set()

    for raw in rows_raw:
        row = raw if isinstance(raw, dict) else {}
        pkg_from = format_package_value(row.get("pkg_from"))
        pkg_to = format_package_value(row.get("pkg_to"))

        if not pkg_from and pkg_to:
            pkg_from = pkg_to
        if not pkg_to and pkg_from:
            pkg_to = pkg_from

        qty_per_crt = max(0.0, to_float(row.get("qty_per_crt"), 0.0))
        no_crts = package_count(pkg_from, pkg_to) if pkg_from and pkg_to else 0
        line_total = qty_per_crt * no_crts
        pkg_text = package_print_text(pkg_from, pkg_to) if (pkg_from or pkg_to) else ""
        pkg_numbers = package_range_numbers(pkg_from, pkg_to) if pkg_from and pkg_to else []
        for n in pkg_numbers:
            unique_packages.add(f"{n:02d}")

        normalized_rows.append(
            {
                "pkg_text": pkg_text,
                "description": normalize_text(row.get("description")),
                "qty_per_crt": qty_per_crt,
                "no_crts": no_crts,
                "line_total": line_total,
                "pkg_numbers": pkg_numbers,
            }
        )

    if not normalized_rows:
        normalized_rows = [
            {
                "pkg_text": "",
                "description": "",
                "qty_per_crt": 0.0,
                "no_crts": 0,
                "line_total": 0.0,
                "pkg_numbers": [],
            }
        ]

    groups: list[dict[str, Any]] = []
    for row in normalized_rows:
        key = row["pkg_text"]
        if not groups or groups[-1]["key"] != key:
            groups.append(
                {
                    "key": key,
                    "rows": [row],
                    "pkg_set": set(row["pkg_numbers"]),
                    "group_total": row["line_total"],
                }
            )
        else:
            groups[-1]["rows"].append(row)
            groups[-1]["pkg_set"].update(row["pkg_numbers"])
            groups[-1]["group_total"] += row["line_total"]

    total_qty = sum(group["group_total"] for group in groups)
    total_packages = len(unique_packages)

    no_pallets_value = to_float(no_pallets_raw, 0.0)
    show_pallets = no_pallets_raw != "" and no_pallets_value > 0

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=9 * mm,
        rightMargin=9 * mm,
        topMargin=9 * mm,
        bottomMargin=15 * mm,
        title=f"Packing List {inv_ref}",
        author="Jimmy Aventus FZE",
    )

    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "base",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.3,
        leading=10.3,
    )
    base_bold = ParagraphStyle(
        "base_bold",
        parent=base,
        fontName="Helvetica-Bold",
    )
    small = ParagraphStyle(
        "small",
        parent=base,
        fontSize=7.9,
        leading=9.4,
    )
    title_style = ParagraphStyle(
        "title_style",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=16,
        alignment=2,
        spaceAfter=0,
        spaceBefore=0,
    )
    meta_label = ParagraphStyle(
        "meta_label",
        parent=base,
        fontName="Helvetica-Bold",
        fontSize=7.6,
        leading=9.0,
    )
    meta_value = ParagraphStyle(
        "meta_value",
        parent=base,
        fontSize=7.9,
        leading=9.2,
    )
    desc_style = ParagraphStyle(
        "desc_style",
        parent=base,
        fontSize=8.0,
        leading=10.0,
    )
    header_center = ParagraphStyle(
        "header_center",
        parent=base_bold,
        alignment=1,  # center
    )
    continued_note_style = ParagraphStyle(
        "continued_note_style",
        parent=small,
        alignment=2,
        fontName="Helvetica-Bold",
        fontSize=8.0,
        leading=9.2,
    )

    story: list[Any] = []

    company_lines = [
        "Jimmy Aventus FZE",
        "Office Q1 06-008/B,",
        "Sharjah Airport Intl Free Zone",
        "Sharjah U.A.E",
        "+971 56 3689970 | cs@jimmyaventus.com",
    ]
    logo_path = BASE_DIR / "logo.png"
    def make_left_header_content() -> Any:
        company_para = Paragraph("<br/>".join(html_escape(line) for line in company_lines), small)
        if logo_path.exists():
            logo = RLImage(str(logo_path), width=20 * mm, height=20 * mm)
            return Table(
                [[logo, company_para]],
                colWidths=[23 * mm, 95 * mm],
                style=TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 0),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                    ]
                ),
            )
        return company_para

    def make_header_table(continued_from_page: int | None = None) -> Any:
        title_rows: list[list[Any]] = [[Paragraph("PACKING LIST", title_style)]]
        if continued_from_page is not None:
            title_rows.append([Paragraph(f"Continued from Page {continued_from_page}", continued_note_style)])

        title_block = Table(
            title_rows,
            colWidths=[74 * mm],
            style=TableStyle(
                [
                    ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            ),
        )

        return Table(
            [[make_left_header_content(), title_block]],
            colWidths=[118 * mm, 74 * mm],
            style=TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            ),
        )

    header_tbl = make_header_table()
    story.append(header_tbl)
    story.append(Spacer(1, 3 * mm))

    billing_block = Table(
        [[Paragraph("<b>Billing Address:</b>", base_bold)], [para_from_multiline(billing_address, base)]],
        colWidths=[74 * mm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ]
        ),
    )
    shipping_block = Table(
        [[Paragraph("<b>Shipping Address:</b>", base_bold)], [para_from_multiline(shipping_address, base)]],
        colWidths=[74 * mm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ]
        ),
    )

    meta_rows = [
        [Paragraph("<b>INV REF</b>", meta_label), Paragraph(":", meta_label), para_from_text(inv_ref, meta_value)],
        [Paragraph("<b>DATE</b>", meta_label), Paragraph(":", meta_label), para_from_text(packing_date, meta_value)],
        [
            Paragraph("<b>TOTAL NUMBER OF PACKAGES/CARTON</b>", meta_label),
            Paragraph(":", meta_label),
            para_from_text(str(total_packages), meta_value),
        ],
        [Paragraph("<b>GROSS WEIGHT</b>", meta_label), Paragraph(":", meta_label), para_from_text(gross_weight, meta_value)],
    ]
    if show_pallets:
        meta_rows.append(
            [Paragraph("<b>NUMBER OF PALLETS</b>", meta_label), Paragraph(":", meta_label), para_from_text(no_pallets_raw, meta_value)]
        )
    meta_table = Table(
        meta_rows,
        colWidths=[33.0 * mm, 3.0 * mm, 16.0 * mm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.0),
            ]
        ),
    )

    details_tbl = Table(
        [[billing_block, shipping_block, meta_table]],
        colWidths=[70 * mm, 70 * mm, 52 * mm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("LEFTPADDING", (2, 0), (2, 0), 0.8 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        ),
    )
    story.append(details_tbl)
    story.append(Spacer(1, 3 * mm))

    table_header: list[Any] = [
        Paragraph("#", header_center),
        Paragraph("PKG/CRT#", header_center),
        Paragraph("ITEM &amp; DESCRIPTION", header_center),
        Paragraph("QTY/CRT", header_center),
        Paragraph("NO OF CRTs", header_center),
        Paragraph("TOTAL QTY IN PCS", header_center),
    ]
    table_widths = [10 * mm, 20 * mm, 95 * mm, 20 * mm, 20 * mm, 27 * mm]
    table_style_cmds: list[tuple[Any, ...]] = [
        ("GRID", (0, 0), (-1, -1), 0.2, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("ALIGN", (0, 1), (1, -1), "CENTER"),
        ("ALIGN", (2, 1), (2, -1), "LEFT"),
        ("ALIGN", (3, 1), (5, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, 0), 8.3),
        ("FONTSIZE", (0, 1), (-1, -1), 8.0),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 1.8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8),
    ]

    group_specs: list[dict[str, Any]] = []
    serial = 1
    for group in groups:
        group_rows = group["rows"]
        group_pkg_count = len(group["pkg_set"])
        group_total_str = format_qty(group["group_total"])
        row_specs: list[dict[str, Any]] = []

        for idx, item in enumerate(group_rows):
            row_spec = {
                "group_id": serial,
                "serial": str(serial),
                "pkg_text": group["key"],
                "description": Paragraph(html_escape(item["description"] or " "), desc_style),
                "qty_per_crt": format_qty(item["qty_per_crt"]),
                "pkg_count": str(group_pkg_count),
                "group_total": group_total_str,
            }
            row_specs.append(row_spec)

        group_specs.append({"rows": row_specs})
        serial += 1

    frame_safety = 4 * mm
    first_page_table_height = doc.height - header_tbl.wrap(doc.width, doc.height)[1] - (3 * mm) - details_tbl.wrap(doc.width, doc.height)[1] - (3 * mm) - frame_safety
    continued_header_sample = make_header_table(1)
    continued_header_height = continued_header_sample.wrap(doc.width, doc.height)[1] + (2 * mm)
    later_page_table_height = doc.height - continued_header_height - frame_safety

    def build_table_row(row_spec: dict[str, Any], show_meta: bool) -> list[Any]:
        return [
            row_spec["serial"] if show_meta else "",
            row_spec["pkg_text"] if show_meta else "",
            row_spec["description"],
            row_spec["qty_per_crt"],
            row_spec["pkg_count"] if show_meta else "",
            row_spec["group_total"] if show_meta else "",
        ]

    def build_chunk_table(chunk_rows: list[dict[str, Any]], merge_cells: bool = True) -> Any:
        table_data = [table_header]
        for entry in chunk_rows:
            table_data.append(entry["cells"])

        span_rules: list[tuple[Any, ...]] = []
        if merge_cells:
            start_index = 0
            while start_index < len(chunk_rows):
                end_index = start_index
                while end_index + 1 < len(chunk_rows) and chunk_rows[end_index + 1]["group_id"] == chunk_rows[start_index]["group_id"]:
                    end_index += 1
                if end_index > start_index:
                    for col in (0, 1, 4, 5):
                        span_rules.append(("SPAN", (col, start_index + 1), (col, end_index + 1)))
                start_index = end_index + 1

        chunk_table = Table(table_data, colWidths=table_widths, repeatRows=1)
        chunk_table.setStyle(TableStyle([*table_style_cmds, *span_rules]))
        return chunk_table

    page_chunks: list[list[dict[str, Any]]] = []
    current_chunk: list[dict[str, Any]] = []
    current_capacity = max(first_page_table_height, 0.0)

    def flush_chunk() -> None:
        nonlocal current_chunk, current_capacity
        if current_chunk:
            page_chunks.append(current_chunk)
        current_chunk = []
        current_capacity = max(later_page_table_height, 0.0)

    for group_spec in group_specs:
        for row_index, row_spec in enumerate(group_spec["rows"]):
            show_meta = row_index == 0 or not current_chunk
            candidate_entry = {
                "group_id": row_spec["group_id"],
                "cells": build_table_row(row_spec, show_meta),
            }
            candidate_chunk = [*current_chunk, candidate_entry]
            flat_candidate_table = build_chunk_table(candidate_chunk, merge_cells=False)
            flat_candidate_height = flat_candidate_table.wrap(doc.width, doc.height)[1]
            flat_candidate_parts = flat_candidate_table.split(doc.width, current_capacity)
            flat_candidate_fits = bool(flat_candidate_parts) and len(flat_candidate_parts) == 1 and flat_candidate_height <= current_capacity

            merged_candidate_table = build_chunk_table(candidate_chunk, merge_cells=True)
            merged_candidate_parts = merged_candidate_table.split(doc.width, current_capacity)
            merged_candidate_fits = bool(merged_candidate_parts) and len(merged_candidate_parts) == 1

            candidate_fits = flat_candidate_fits and merged_candidate_fits

            if current_chunk and not candidate_fits:
                flush_chunk()
                candidate_entry = {
                    "group_id": row_spec["group_id"],
                    "cells": build_table_row(row_spec, True),
                }
                current_chunk.append(candidate_entry)
            else:
                current_chunk = candidate_chunk

    flush_chunk()

    if page_chunks:
        story.append(build_chunk_table(page_chunks[0]))
        for chunk_index, chunk_rows in enumerate(page_chunks[1:], start=2):
            story.append(PageBreak())
            story.append(make_header_table(chunk_index - 1))
            story.append(Spacer(1, 4 * mm))
            story.append(build_chunk_table(chunk_rows))

    story.append(Spacer(1, 2.5 * mm))
    story.append(Paragraph(f"<b>TOTAL QUANTITY (PCS):</b> {html_escape(format_qty(total_qty))}", base_bold))

    if normalize_text(remarks):
        story.append(Spacer(1, 1.5 * mm))
        story.append(Paragraph("<b>REMARKS:</b>", base_bold))
        story.append(para_from_multiline(remarks, base))

    if normalize_text(notes):
        story.append(Spacer(1, 1.5 * mm))
        story.append(Paragraph("<b>NOTES:</b>", base_bold))
        story.append(para_from_multiline(notes, base))

    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph("For Jimmy Aventus FZE", base))
    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph("Authorized Signatory", base))

    doc.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()


def build_delivery_note_pdf_bytes(payload: dict[str, Any]) -> bytes:
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("PDF engine unavailable on server.")

    data = payload if isinstance(payload, dict) else {}
    inv_ref = normalize_text(data.get("inv_ref")) or "delivery-note"
    packing_date = normalize_text(data.get("packing_date"))
    gross_weight = maybe_add_kg(normalize_text(data.get("gross_weight")))
    no_pallets_raw = normalize_text(data.get("no_pallets"))
    billing_address = normalize_multiline_text(data.get("billing_address"))
    shipping_address = normalize_multiline_text(data.get("shipping_address"))
    rows_raw = data.get("rows") if isinstance(data.get("rows"), list) else []

    unique_packages: set[str] = set()
    for raw in rows_raw:
        row = raw if isinstance(raw, dict) else {}
        pkg_from = format_package_value(row.get("pkg_from"))
        pkg_to = format_package_value(row.get("pkg_to"))
        if not pkg_from and pkg_to:
            pkg_from = pkg_to
        if not pkg_to and pkg_from:
            pkg_to = pkg_from
        for n in package_range_numbers(pkg_from, pkg_to):
            unique_packages.add(f"{n:02d}")
    total_packages = len(unique_packages)
    no_pallets_value = to_float(no_pallets_raw, 0.0)
    show_pallets = no_pallets_raw != "" and no_pallets_value > 0

    summary_items = summarize_delivery_items(rows_raw)
    if not summary_items:
        raise ValueError("No item descriptions found to summarize.")

    total_qty = sum(to_float(item.get("total_qty"), 0.0) for item in summary_items)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=9 * mm,
        rightMargin=9 * mm,
        topMargin=9 * mm,
        bottomMargin=15 * mm,
        title=f"Delivery Note {inv_ref}",
        author="Jimmy Aventus FZE",
    )

    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "dn_base",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.3,
        leading=10.3,
    )
    base_bold = ParagraphStyle(
        "dn_base_bold",
        parent=base,
        fontName="Helvetica-Bold",
    )
    small = ParagraphStyle(
        "dn_small",
        parent=base,
        fontSize=7.9,
        leading=9.4,
    )
    title_style = ParagraphStyle(
        "dn_title",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=16,
        alignment=2,
        spaceAfter=0,
        spaceBefore=0,
    )
    meta_label = ParagraphStyle(
        "dn_meta_label",
        parent=base,
        fontName="Helvetica-Bold",
        fontSize=7.6,
        leading=9.0,
    )
    meta_value = ParagraphStyle(
        "dn_meta_value",
        parent=base,
        fontSize=7.9,
        leading=9.2,
    )
    header_center = ParagraphStyle(
        "dn_header_center",
        parent=base_bold,
        alignment=1,
    )
    desc_style = ParagraphStyle(
        "dn_desc",
        parent=base,
        fontSize=8.0,
        leading=10.0,
    )

    story: list[Any] = []

    company_lines = [
        "Jimmy Aventus FZE",
        "Office Q1 06-008/B,",
        "Sharjah Airport Intl Free Zone",
        "Sharjah U.A.E",
        "+971 56 3689970 | cs@jimmyaventus.com",
    ]
    company_para = Paragraph("<br/>".join(html_escape(line) for line in company_lines), small)

    logo_path = BASE_DIR / "logo.png"
    left_header_content: Any
    if logo_path.exists():
        logo = RLImage(str(logo_path), width=20 * mm, height=20 * mm)
        left_header_content = Table(
            [[logo, company_para]],
            colWidths=[23 * mm, 95 * mm],
            style=TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            ),
        )
    else:
        left_header_content = company_para

    header_tbl = Table(
        [[left_header_content, Paragraph("DELIVERY NOTE", title_style)]],
        colWidths=[118 * mm, 74 * mm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        ),
    )
    story.append(header_tbl)
    story.append(Spacer(1, 3 * mm))

    billing_block = Table(
        [[Paragraph("<b>Billing Address:</b>", base_bold)], [para_from_multiline(billing_address, base)]],
        colWidths=[70 * mm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ]
        ),
    )
    shipping_block = Table(
        [[Paragraph("<b>Shipping Address:</b>", base_bold)], [para_from_multiline(shipping_address, base)]],
        colWidths=[70 * mm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ]
        ),
    )

    meta_rows = [
        [Paragraph("<b>INV REF</b>", meta_label), Paragraph(":", meta_label), para_from_text(inv_ref, meta_value)],
        [Paragraph("<b>DATE</b>", meta_label), Paragraph(":", meta_label), para_from_text(packing_date, meta_value)],
        [
            Paragraph("<b>TOTAL NUMBER OF PACKAGES/CARTON</b>", meta_label),
            Paragraph(":", meta_label),
            para_from_text(str(total_packages), meta_value),
        ],
        [Paragraph("<b>GROSS WEIGHT</b>", meta_label), Paragraph(":", meta_label), para_from_text(gross_weight, meta_value)],
    ]
    if show_pallets:
        meta_rows.append(
            [Paragraph("<b>NUMBER OF PALLETS</b>", meta_label), Paragraph(":", meta_label), para_from_text(no_pallets_raw, meta_value)]
        )
    meta_table = Table(
        meta_rows,
        colWidths=[33.0 * mm, 3.0 * mm, 16.0 * mm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.0),
            ]
        ),
    )

    details_tbl = Table(
        [[billing_block, shipping_block, meta_table]],
        colWidths=[70 * mm, 70 * mm, 52 * mm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("LEFTPADDING", (2, 0), (2, 0), 0.8 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        ),
    )
    story.append(details_tbl)
    story.append(Spacer(1, 3 * mm))

    table_data: list[list[Any]] = [
        [
            Paragraph("#", header_center),
            Paragraph("ITEM &amp; DESCRIPTION", header_center),
            Paragraph("TOTAL QTY IN PCS", header_center),
        ]
    ]
    for idx, item in enumerate(summary_items, start=1):
        table_data.append(
            [
                str(idx),
                Paragraph(html_escape(normalize_text(item.get("description")) or " "), desc_style),
                format_qty(item.get("total_qty")),
            ]
        )

    summary_table = Table(table_data, colWidths=[12 * mm, 138 * mm, 42 * mm], repeatRows=1)
    summary_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.2, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                ("ALIGN", (0, 1), (0, -1), "CENTER"),
                ("ALIGN", (1, 1), (1, -1), "LEFT"),
                ("ALIGN", (2, 1), (2, -1), "CENTER"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, 0), 8.3),
                ("FONTSIZE", (0, 1), (-1, -1), 8.0),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 1.8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8),
            ]
        )
    )
    story.append(summary_table)
    story.append(Spacer(1, 2.5 * mm))
    story.append(Paragraph(f"<b>TOTAL QUANTITY (PCS):</b> {html_escape(format_qty(total_qty))}", base_bold))

    story.append(Spacer(1, 18 * mm))
    story.append(Paragraph("For Jimmy Aventus FZE", base))
    story.append(Spacer(1, 14 * mm))
    story.append(Paragraph("Authorized Signatory", base))

    doc.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()


@app.route("/")
def index() -> Any:
    return send_from_directory(BASE_DIR, HTML_FILE)


@app.route("/<path:filename>")
def static_files(filename: str) -> Any:
    return send_from_directory(BASE_DIR, filename)


@app.route("/api/health")
def health() -> Any:
    return jsonify({"ok": True, "db_path": str(DB_PATH)})


@app.get("/api/packing-lists")
def list_packing_lists() -> Any:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, updated_at, data_json
            FROM packing_lists
            ORDER BY datetime(updated_at) DESC, id ASC
            """
        ).fetchall()

    output = []
    for row in rows:
        try:
            data = json.loads(row["data_json"]) if row["data_json"] else {}
        except json.JSONDecodeError:
            data = {}
        output.append(
            {
                "id": row["id"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "data": data,
            }
        )
    return jsonify(output)


@app.post("/api/packing-lists/save")
def save_packing_list() -> Any:
    payload = request.get_json(silent=True) or {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    rec_id = normalize_text(payload.get("id")) or normalize_text(data.get("inv_ref"))
    if not rec_id:
        return jsonify({"success": False, "error": "id is required"}), 400

    data["inv_ref"] = rec_id
    now = utc_now_iso()
    created_at = normalize_text(payload.get("createdAt")) or now
    updated_at = normalize_text(payload.get("updatedAt")) or now
    data_json = json.dumps(data, ensure_ascii=False)

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT created_at FROM packing_lists WHERE id = ?",
            (rec_id,),
        ).fetchone()
        if existing and existing["created_at"]:
            created_at = existing["created_at"]

        conn.execute(
            """
            INSERT INTO packing_lists (id, created_at, updated_at, data_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              updated_at = excluded.updated_at,
              data_json = excluded.data_json
            """,
            (rec_id, created_at, updated_at, data_json),
        )
        conn.commit()

    return jsonify({"success": True, "id": rec_id})


@app.delete("/api/packing-lists/<path:record_id>")
def delete_packing_list(record_id: str) -> Any:
    rec_id = normalize_text(record_id)
    if not rec_id:
        return jsonify({"success": False, "error": "id is required"}), 400

    with get_conn() as conn:
        cur = conn.execute("DELETE FROM packing_lists WHERE id = ?", (rec_id,))
        conn.commit()
    return jsonify({"success": True, "deleted": cur.rowcount > 0})


@app.get("/api/items")
def list_items() -> Any:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT description FROM item_descriptions ORDER BY description COLLATE NOCASE ASC"
        ).fetchall()
    return jsonify([row["description"] for row in rows])


@app.post("/api/items/replace")
def replace_items() -> Any:
    payload = request.get_json(silent=True) or {}
    items = payload.get("items")
    if not isinstance(items, list):
        return jsonify({"success": False, "error": "items must be an array"}), 400

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = normalize_text(item)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    cleaned.sort(key=lambda x: x.lower())

    now = utc_now_iso()
    with get_conn() as conn:
        conn.execute("DELETE FROM item_descriptions")
        conn.executemany(
            "INSERT INTO item_descriptions (description, updated_at) VALUES (?, ?)",
            [(item, now) for item in cleaned],
        )
        conn.commit()
    return jsonify({"success": True, "count": len(cleaned)})


@app.get("/get_addresses")
def get_addresses() -> Any:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name FROM addresses ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()
    return jsonify([{"id": row["id"], "name": row["name"]} for row in rows])


@app.get("/search_addresses")
def search_addresses() -> Any:
    query = normalize_text(request.args.get("q"))
    like = f"%{query}%"
    with get_conn() as conn:
        if query:
            rows = conn.execute(
                """
                SELECT id, name, billing_address, shipping_address
                FROM addresses
                WHERE name LIKE ? COLLATE NOCASE
                ORDER BY name COLLATE NOCASE ASC
                LIMIT 100
                """,
                (like,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, name, billing_address, shipping_address
                FROM addresses
                ORDER BY name COLLATE NOCASE ASC
                LIMIT 100
                """
            ).fetchall()
    return jsonify(
        [
            {
                "id": row["id"],
                "name": row["name"],
                "billing_address": row["billing_address"],
                "shipping_address": row["shipping_address"],
            }
            for row in rows
        ]
    )


@app.get("/get_address/<address_id>")
def get_address(address_id: str) -> Any:
    try:
        aid = int(address_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid address id"}), 400

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, name, billing_address, shipping_address
            FROM addresses
            WHERE id = ?
            """,
            (aid,),
        ).fetchone()
    if not row:
        return jsonify({"success": False, "error": "Address not found"}), 404

    return jsonify(
        {
            "id": row["id"],
            "name": row["name"],
            "billing_address": row["billing_address"],
            "shipping_address": row["shipping_address"],
        }
    )


@app.post("/save_address")
def save_address() -> Any:
    payload = request.get_json(silent=True) or {}
    name = normalize_text(payload.get("name"))
    if not name:
        return jsonify({"success": False, "error": "name is required"}), 400

    billing = str(payload.get("billing_address") or "")
    shipping = str(payload.get("shipping_address") or "")
    now = utc_now_iso()

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO addresses (name, billing_address, shipping_address, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              billing_address = excluded.billing_address,
              shipping_address = excluded.shipping_address,
              updated_at = excluded.updated_at
            """,
            (name, billing, shipping, now),
        )
        row = conn.execute(
            "SELECT id, name FROM addresses WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        conn.commit()

    return jsonify({"success": True, "id": row["id"], "name": row["name"]})


@app.put("/update_address/<address_id>")
def update_address(address_id: str) -> Any:
    try:
        aid = int(address_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid address id"}), 400

    payload = request.get_json(silent=True) or {}
    name = normalize_text(payload.get("name"))
    if not name:
        return jsonify({"success": False, "error": "name is required"}), 400

    billing = str(payload.get("billing_address") or "")
    shipping = str(payload.get("shipping_address") or "")
    now = utc_now_iso()

    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE addresses
            SET name = ?, billing_address = ?, shipping_address = ?, updated_at = ?
            WHERE id = ?
            """,
            (name, billing, shipping, now, aid),
        )
        conn.commit()
    if cur.rowcount == 0:
        return jsonify({"success": False, "error": "Address not found"}), 404
    return jsonify({"success": True})


@app.delete("/delete_address/<address_id>")
def delete_address(address_id: str) -> Any:
    try:
        aid = int(address_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid address id"}), 400

    with get_conn() as conn:
        cur = conn.execute("DELETE FROM addresses WHERE id = ?", (aid,))
        conn.commit()
    return jsonify({"success": True, "deleted": cur.rowcount > 0})


@app.post("/api/packing-pdf")
def generate_packing_pdf() -> Any:
    if not REPORTLAB_AVAILABLE:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Server PDF engine is unavailable. Install reportlab and restart the app.",
                }
            ),
            503,
        )

    payload = request.get_json(silent=True) or {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid payload."}), 400

    try:
        pdf_bytes = build_packing_pdf_bytes(data)
    except Exception as exc:
        return jsonify({"success": False, "error": f"Failed to generate PDF: {exc}"}), 500

    inv_ref = normalize_text(data.get("inv_ref")) or "packing-list"
    filename = f"Packing_List_{inv_ref}.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.post("/api/delivery-note-pdf")
def generate_delivery_note_pdf() -> Any:
    if not REPORTLAB_AVAILABLE:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Server PDF engine is unavailable. Install reportlab and restart the app.",
                }
            ),
            503,
        )

    payload = request.get_json(silent=True) or {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid payload."}), 400

    try:
        pdf_bytes = build_delivery_note_pdf_bytes(data)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"success": False, "error": f"Failed to generate delivery note PDF: {exc}"}), 500

    inv_ref = normalize_text(data.get("inv_ref")) or "delivery-note"
    filename = f"Delivery_Note_{inv_ref}.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    app.run(host="127.0.0.1", port=port, debug=True)
