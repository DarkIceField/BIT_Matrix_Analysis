from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from dataclasses import dataclass, asdict
from io import BytesIO
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


DEFAULT_TEXT = "资料免费共享｜禁止倒卖｜GitHub: DarkIceField/BIT_Matrix_Analysis"
DEFAULT_COLOR = "#6F6F6F"
DEFAULT_ALPHA = 0.18
DEFAULT_ANGLE = -35
EXAM_ROOT = Path("试卷")
PDF_TTF_FONT = "WatermarkFont"
PDF_CID_FALLBACK_FONT = "STSong-Light"
WATERMARK_METADATA = {
    "/WatermarkTool": "BIT_Matrix_Analysis GitHub Actions",
    "/WatermarkText": DEFAULT_TEXT,
    "/WatermarkVersion": "1",
    "/Watermarked": "true",
}


@dataclass
class ProcessResult:
    path: str
    action: str


def select_exam_pdfs(paths: Iterable[Path]) -> list[Path]:
    selected: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        relative = _repo_relative_path(path)
        if not _is_exam_pdf(relative):
            continue
        key = relative.as_posix()
        if key not in seen:
            selected.append(relative)
            seen.add(key)
    return selected


def has_watermark_marker(path: Path) -> bool:
    metadata = dict(PdfReader(str(path)).metadata or {})
    return (
        metadata.get("/Watermarked") == WATERMARK_METADATA["/Watermarked"]
        and metadata.get("/WatermarkVersion") == WATERMARK_METADATA["/WatermarkVersion"]
        and metadata.get("/WatermarkTool") == WATERMARK_METADATA["/WatermarkTool"]
    )


def mark_pdf_metadata_only(path: Path) -> bool:
    if has_watermark_marker(path):
        return False

    reader = PdfReader(str(path))
    writer = PdfWriter(clone_from=reader)
    writer.add_metadata(_metadata_with_marker(reader.metadata))
    _write_pdf(writer, path)
    return True


def watermark_pdf(path: Path, text: str = DEFAULT_TEXT) -> bool:
    if has_watermark_marker(path):
        return False

    reader = PdfReader(str(path))
    writer = PdfWriter(clone_from=reader)
    for page in writer.pages:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        overlay = _make_pdf_overlay(width, height, text)
        page.merge_page(overlay, over=True)

    writer.add_metadata(_metadata_with_marker(reader.metadata, text=text))
    _write_pdf(writer, path)
    return True


def collect_all_existing_pdfs() -> list[Path]:
    if not EXAM_ROOT.exists():
        return []
    return select_exam_pdfs(path for path in EXAM_ROOT.rglob("*") if path.is_file())


def collect_changed_pdfs(before: str, after: str) -> list[Path]:
    if _is_zero_sha(before):
        command = ["git", "ls-files", "-z", "--", ":(glob)试卷/**/*.pdf", ":(glob)试卷/**/*.PDF"]
    else:
        command = [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=AM",
            "-z",
            before,
            after,
            "--",
            ":(glob)试卷/**/*.pdf",
            ":(glob)试卷/**/*.PDF",
        ]
    completed = subprocess.run(command, check=True, stdout=subprocess.PIPE)
    paths = [Path(item) for item in completed.stdout.decode("utf-8").split("\0") if item]
    return select_exam_pdfs(paths)


def process_pdfs(paths: Iterable[Path], *, metadata_only: bool = False, dry_run: bool = False) -> list[ProcessResult]:
    results: list[ProcessResult] = []
    for relative in select_exam_pdfs(paths):
        path = relative if relative.is_absolute() else Path.cwd() / relative
        if not path.exists():
            results.append(ProcessResult(relative.as_posix(), "missing"))
            continue
        if has_watermark_marker(path):
            results.append(ProcessResult(relative.as_posix(), "skipped"))
            continue
        if dry_run:
            results.append(ProcessResult(relative.as_posix(), "would_mark_metadata" if metadata_only else "would_watermark"))
            continue
        changed = mark_pdf_metadata_only(path) if metadata_only else watermark_pdf(path)
        results.append(ProcessResult(relative.as_posix(), "marked_metadata" if metadata_only and changed else "watermarked"))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watermark exam PDFs and mark processed files in PDF metadata.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--all-existing", action="store_true", help="Process all existing PDFs under 试卷/.")
    source.add_argument("--paths", nargs="*", type=Path, help="Explicit PDF paths to process.")
    source.add_argument("--paths-from", type=Path, help="Read NUL-separated paths from a file.")
    source.add_argument("--changed-from", help="Git base SHA for changed PDF detection.")
    parser.add_argument("--changed-to", help="Git head SHA for changed PDF detection.")
    parser.add_argument("--metadata-only", action="store_true", help="Only add idempotency metadata; do not add visible watermarks.")
    parser.add_argument("--dry-run", action="store_true", help="Report planned actions without writing PDFs.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    if args.all_existing:
        paths = collect_all_existing_pdfs()
    elif args.paths is not None:
        paths = args.paths
    elif args.paths_from:
        paths = _read_nul_paths(args.paths_from)
    else:
        if not args.changed_to:
            parser.error("--changed-to is required with --changed-from")
        paths = collect_changed_pdfs(args.changed_from, args.changed_to)

    results = process_pdfs(paths, metadata_only=args.metadata_only, dry_run=args.dry_run)
    payload = {
        "metadata_only": args.metadata_only,
        "dry_run": args.dry_run,
        "total": len(results),
        "changed": sum(1 for item in results if item.action in {"watermarked", "marked_metadata"}),
        "results": [asdict(item) for item in results],
    }
    _print_payload(payload, as_json=args.json)
    return 0


def _repo_relative_path(path: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate
    try:
        return candidate.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return candidate


def _is_exam_pdf(path: Path) -> bool:
    parts = path.parts
    return len(parts) >= 2 and parts[0] == EXAM_ROOT.name and path.suffix.lower() == ".pdf"


def _is_zero_sha(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"0+", value))


def _read_nul_paths(path: Path) -> list[Path]:
    return [Path(item) for item in path.read_bytes().decode("utf-8").split("\0") if item]


def _metadata_with_marker(metadata, text: str = DEFAULT_TEXT) -> dict[str, str]:
    merged: dict[str, str] = {}
    for key, value in dict(metadata or {}).items():
        if str(key).startswith("/") and value is not None:
            merged[str(key)] = str(value)
    merged.update(WATERMARK_METADATA)
    merged["/WatermarkText"] = text
    return merged


def _write_pdf(writer: PdfWriter, path: Path) -> None:
    tmp = path.with_name(f".{path.name}.watermark.tmp{path.suffix}")
    with tmp.open("wb") as output:
        writer.write(output)
    tmp.replace(path)


def _make_pdf_overlay(width: float, height: float, text: str):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(width, height))
    font_name = _register_pdf_font()
    font_size = _fit_pdf_font_size(text, width, height, font_name)
    pdf.setFont(font_name, font_size)
    pdf.setFillColor(HexColor(DEFAULT_COLOR))
    pdf.setFillAlpha(DEFAULT_ALPHA)

    for x, y in _watermark_centers(width, height):
        pdf.saveState()
        pdf.translate(x, y)
        pdf.rotate(DEFAULT_ANGLE)
        pdf.drawCentredString(0, 0, text)
        pdf.restoreState()

    pdf.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def _register_pdf_font() -> str:
    try:
        pdfmetrics.getFont(PDF_TTF_FONT)
        return PDF_TTF_FONT
    except KeyError:
        pass

    font_path = _find_ttf_font()
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont(PDF_TTF_FONT, str(font_path), subfontIndex=0))
            return PDF_TTF_FONT
        except Exception:
            pass

    try:
        pdfmetrics.getFont(PDF_CID_FALLBACK_FONT)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(PDF_CID_FALLBACK_FONT))
    return PDF_CID_FALLBACK_FONT


def _find_ttf_font() -> Path | None:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/arphic/ukai.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    return next((path for path in candidates if path.exists()), None)


def _fit_pdf_font_size(text: str, width: float, height: float, font_name: str) -> int:
    diagonal = math.hypot(width, height)
    size = max(16, min(30, int(min(width, height) / 20)))
    while size > 12 and pdfmetrics.stringWidth(text, font_name, size) > diagonal * 0.8:
        size -= 1
    return size


def _watermark_centers(width: float, height: float) -> list[tuple[float, float]]:
    return [
        (width * 0.25, height * 0.22),
        (width * 0.75, height * 0.22),
        (width * 0.25, height * 0.50),
        (width * 0.75, height * 0.50),
        (width * 0.25, height * 0.78),
        (width * 0.75, height * 0.78),
    ]


def _print_payload(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"Total PDFs: {payload['total']}")
    print(f"Changed PDFs: {payload['changed']}")
    for item in payload["results"]:
        print(f"{item['action']}: {item['path']}")


if __name__ == "__main__":
    raise SystemExit(main())
