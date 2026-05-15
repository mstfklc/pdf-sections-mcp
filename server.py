#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp[cli]>=1.2",
#   "pypdf>=4.0",
#   "markitdown[pdf]>=0.0.1a25",
# ]
# ///
"""PDF Sections MCP — Slice a page range from a PDF, split it into sections,
and write each section as a structured JSON file.

Pipeline:
  1. Crop the PDF to the requested page range.
  2. Convert the cropped PDF to Markdown via markitdown.
  3. Detect sections (auto-detect numbered headings, or use a user-supplied
     list of explicit section titles).
  4. Emit one JSON file per section. The JSON schema is flexible — callers
     pass arbitrary labels and parent headers; nothing is hard-coded.

Design notes:
- No field is required. `labels` such as `class`, `subject` are entirely
  user-defined.
- `header_N` keys are filled from `parent_headers` + the detected section
  title (placed at the next available `header_N` slot).
- Default output directory is `~/Desktop/<pdf-stem>-sections/`.
- The raw markdown is saved as `_full.md` and the cropped PDF as `_split.pdf`
  to enable later validation and re-runs without re-cropping the source.

Tools exposed:
- split_pdf          → crop only (PDF in → PDF out)
- preview_sections   → detect and list sections without writing files
- extract_sections   → full pipeline: crop + convert + detect + write JSONs
- validate_sections  → verify saved JSONs against fresh extraction; fix
                       truncated / mismatched content on request
- detect_page_offset → find the difference between PDF physical page and
                       book printed page
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import SamplingMessage, TextContent
from pypdf import PdfReader, PdfWriter

mcp = FastMCP("pdf-sections")


DEFAULT_SUMMARY_PROMPT = (
    "Summarize the following textbook section in a single flowing paragraph of "
    "roughly 150–220 words. Cover the main concepts, key examples, important "
    "formulas or equations, and any concrete real-world applications mentioned. "
    "Match the language of the source text exactly. Return only the summary "
    "paragraph itself — no preamble, no headings, no bullet points, no quoting.\n\n"
    "Section content:\n---\n{content}\n---"
)


async def _generate_summary(
    ctx: Context,
    content: str,
    prompt_template: str,
    max_tokens: int,
) -> tuple[str, str | None]:
    """Request a summary from the MCP client's LLM via sampling.

    Returns (summary, error). When the client supports sampling, `error` is
    None and `summary` carries the generated text. When sampling is rejected
    or fails for any reason, `summary` is "" and `error` describes why so the
    caller can surface it in the tool response.
    """
    prompt = prompt_template.format(content=content)
    try:
        result = await ctx.session.create_message(
            messages=[
                SamplingMessage(
                    role="user",
                    content=TextContent(type="text", text=prompt),
                )
            ],
            max_tokens=max_tokens,
        )
    except Exception as exc:
        return "", f"sampling_failed: {exc}"

    out = result.content
    if isinstance(out, TextContent):
        return out.text.strip(), None
    # Some clients return a list of content items
    if isinstance(out, list):
        chunks = [c.text for c in out if isinstance(c, TextContent)]
        if chunks:
            return "\n".join(chunks).strip(), None
    return "", "sampling_returned_no_text"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expand(p: str | Path) -> Path:
    """Expand ~ and resolve to an absolute path."""
    return Path(p).expanduser().resolve()


def _default_desktop() -> Path:
    """Locate the user's Desktop directory across macOS, Windows, and Linux.

    Handles common edge cases:
      * Windows with OneDrive enabled (Desktop redirected to OneDrive\\Desktop).
      * Windows where the Shell Folders registry key points elsewhere.
      * Linux with non-English locales (uses `xdg-user-dir DESKTOP`).
      * Falls back to ~/Desktop and creates it if nothing else exists.
    """
    home = Path.home()

    if sys.platform == "win32":
        # Authoritative source on Windows: Shell Folders registry entry.
        try:
            import winreg  # type: ignore[import-not-found]
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
                expanded = os.path.expandvars(value)
                candidate = Path(expanded)
                if candidate.exists():
                    return candidate
        except Exception:
            pass
        # Fallback: OneDrive\Desktop, then plain Desktop.
        for candidate in (home / "OneDrive" / "Desktop", home / "Desktop"):
            if candidate.exists():
                return candidate
        return home / "Desktop"

    # macOS and Linux: try ~/Desktop first.
    desktop = home / "Desktop"
    if desktop.exists():
        return desktop

    if sys.platform.startswith("linux"):
        # Honour XDG: locale-translated dir names (e.g. ~/Bureau in French).
        try:
            import subprocess
            result = subprocess.run(
                ["xdg-user-dir", "DESKTOP"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if result.returncode == 0:
                candidate = Path(result.stdout.strip())
                if candidate.exists():
                    return candidate
        except Exception:
            pass

    # Last resort: create ~/Desktop (works on every supported platform).
    desktop.mkdir(parents=True, exist_ok=True)
    return desktop


def _parse_page_range(range_str: str) -> list[int]:
    """Parse '1-3,5,7-10' into [1,2,3,5,7,8,9,10]. Pages are 1-indexed."""
    pages: set[int] = set()
    for part in range_str.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"Invalid range: '{part}'")
            start, end = int(a), int(b)
            if start > end:
                raise ValueError(f"Invalid range (start > end): '{part}'")
            pages.update(range(start, end + 1))
        else:
            if not part.isdigit():
                raise ValueError(f"Invalid page: '{part}'")
            pages.add(int(part))
    if not pages:
        raise ValueError("Page range is empty")
    return sorted(pages)


# Slug map supports common Turkish + Western European diacritics so the tool
# is useful beyond English-only PDFs.
_SLUG_TR_MAP = str.maketrans({
    "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
    "â": "a", "î": "i", "û": "u",
    "ñ": "n", "ó": "o", "é": "e", "è": "e", "à": "a", "á": "a",
    "ä": "a", "ë": "e", "ï": "i", "ý": "y", "ÿ": "y",
})


def _slugify(text: str, max_len: int = 70) -> str:
    """Convert a section title to a filesystem-safe slug."""
    # Strip a leading section number like "1.1.3."
    text = re.sub(r"^\d+(?:\.\d+)+\.?\s*", "", text).strip()
    text = text.lower().translate(_SLUG_TR_MAP)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    return text[:max_len] or "section"


def _extract_section_number(title: str) -> str | None:
    """Return '1.1.3' from '1.1.3. Mol Concept'."""
    m = re.match(r"^(\d+(?:\.\d+)+)\.?\s+", title)
    return m.group(1) if m else None


def _split_pdf_to_file(
    source: Path, pages: list[int], destination: Path,
) -> tuple[int, int]:
    """Write the specified pages from `source` PDF to `destination`.

    Returns (pages_written, source_total_pages).
    """
    reader = PdfReader(str(source))
    total = len(reader.pages)
    for p in pages:
        if p < 1 or p > total:
            raise ValueError(
                f"Page {p} out of range (source PDF has {total} pages)"
            )
    writer = PdfWriter()
    for p in pages:
        writer.add_page(reader.pages[p - 1])
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "wb") as f:
        writer.write(f)
    return len(pages), total


def _pdf_to_markdown(pdf_path: Path) -> str:
    """Convert PDF to markdown text using markitdown."""
    from markitdown import MarkItDown
    md = MarkItDown()
    result = md.convert(str(pdf_path))
    return result.text_content


# Section detection ----------------------------------------------------------


# Heading must look like a textbook section number:
#   * first segment: 1–99 (no leading zero — rejects "0.1", "01.2", and accidental
#     matches like a numeric quantity at the start of a line)
#   * subsequent segments: 1–2 digits — this is what rejects thousands-separator
#     numbers like "40.000" or "10.000", which otherwise look like "X.YYY"
#     section identifiers to a naive regex.
#   * heading may appear at the start of a line OR mid-line after sentence-ending
#     punctuation (".!?;:" + whitespace). This handles PDF extractors that collapse
#     line breaks and concatenate a new section title onto the end of a paragraph.
#   * title must begin with an uppercase letter (Latin or Turkish) — keeps body
#     text and lowercase fragments from being mistaken for a heading.
#   * title is bounded by the next sentence punctuation OR 80 characters, whichever
#     comes first, so we don't capture half a paragraph when line breaks are lost.
_NUMBERED_HEADING_RE = re.compile(
    r"(?:^|(?<=[\.\!\?;:])\s+)"
    r"([1-9]\d?(?:\.\d{1,2}){1,5})\.?\s+"
    r"([A-ZÇĞİÖŞÜ][^\n.!?]{2,80})",
    re.MULTILINE,
)


def _looks_like_section_title(title: str) -> bool:
    """Reject obvious false positives where the captured "title" is actually
    the tail of a sentence (sentence-ending punctuation, etc.)."""
    title = title.strip()
    if not title:
        return False
    if title[-1] in ".!?;:":
        return False
    return True


def _auto_detect_numbered_sections(text: str) -> list[dict[str, Any]]:
    """Detect numbered sections like '1.1.3. Mol Concept'.

    Returns a list of {number, title, title_only, start, end, content}.
    """
    matches = [
        m for m in _NUMBERED_HEADING_RE.finditer(text)
        if _looks_like_section_title(m.group(2))
    ]
    if not matches:
        return []
    sections: list[dict[str, Any]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # Normalise internal whitespace (PDF extractors often inject tabs
        # between words of a heading).
        title_only = re.sub(r"\s+", " ", m.group(2).strip())
        title = re.sub(r"\s+", " ", m.group(0).strip())
        sections.append({
            "number": m.group(1),
            "title": title,
            "title_only": title_only,
            "start": start,
            "end": end,
            "content": text[start:end].strip(),
        })
    return sections


def _split_by_titles(
    text: str, titles: list[str],
) -> list[dict[str, Any]]:
    """Split markdown text using an ordered list of explicit section titles.

    Each title is located via substring match; sections span between
    consecutive title positions. Titles not found are reported back to the
    caller so they can correct typos or character mismatches.
    """
    positions: list[tuple[int, str]] = []
    not_found: list[str] = []
    for title in titles:
        idx = text.find(title)
        if idx >= 0:
            positions.append((idx, title))
        else:
            not_found.append(title)
    if not positions:
        return [{
            "_not_found": not_found,
            "number": None,
            "title": "",
            "title_only": "",
            "content": "",
        }] if not_found else []
    positions.sort(key=lambda x: x[0])
    sections: list[dict[str, Any]] = []
    for i, (start, title) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        sections.append({
            "number": _extract_section_number(title),
            "title": title,
            "title_only": re.sub(r"^\d+(?:\.\d+)+\.?\s+", "", title).strip(),
            "start": start,
            "end": end,
            "content": text[start:end].strip(),
            "_not_found": not_found,
        })
        not_found = []  # attach only to the first section
    return sections


def _next_header_index(obj: dict[str, Any]) -> int:
    """Find the next available `header_N` slot in the object."""
    used: list[int] = []
    for k in obj.keys():
        if k.startswith("header_"):
            tail = k[len("header_"):]
            if tail.isdigit():
                used.append(int(tail))
    return (max(used) + 1) if used else 1


# Validation helpers ---------------------------------------------------------


def _normalize_for_compare(text: str) -> str:
    """Reduce whitespace runs and trim — used for fuzzy comparison."""
    return re.sub(r"\s+", " ", text).strip()


def _looks_truncated(text: str) -> bool:
    """Heuristic check for content that appears cut off mid-sentence."""
    stripped = text.rstrip()
    if not stripped:
        return False
    last_char = stripped[-1]
    # Properly terminated: ends with sentence-ending punctuation, list marker,
    # closing fence, table separator, or closing parenthesis.
    if last_char in ".!?\"'»)]}>`":
        return False
    if stripped.endswith("```"):
        return False
    # Otherwise probably truncated
    return True


def _has_replacement_chars(text: str) -> bool:
    """Detect PDF extraction artifacts like the replacement character (U+FFFD)
    or stray (cid:NN) markers, which indicate font/encoding problems."""
    return "�" in text or "(cid:" in text


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def split_pdf(
    pdf_path: str,
    page_range: str,
    output_path: str | None = None,
    page_offset: int = 0,
) -> dict[str, Any]:
    """Crop a PDF to a page range and save the result as a new PDF.

    Parameters:
      pdf_path:    Source PDF (absolute path or `~` expanded).
      page_range:  Page range — for example "47-76", "5,7,9-12", "1-3,5,8".
      output_path: Destination PDF path. If omitted, saves to
                   `~/Desktop/<pdf-stem>-<range>.pdf`.
      page_offset: Added to every requested page (use this when the printed
                   book page differs from the PDF's physical page — e.g.,
                   book page 47 == PDF page 48 means `page_offset=1`).

    Returns the output path, pages written, and the source PDF total.
    """
    src = _expand(pdf_path)
    if not src.exists():
        raise FileNotFoundError(f"PDF not found: {src}")
    pages = _parse_page_range(page_range)
    if page_offset:
        pages = [p + page_offset for p in pages]
    if output_path is None:
        safe_range = page_range.replace(",", "_").replace(" ", "")
        out = _default_desktop() / f"{src.stem}-{safe_range}.pdf"
    else:
        out = _expand(output_path)
    written, total = _split_pdf_to_file(src, pages, out)
    return {
        "status": "ok",
        "output_path": str(out),
        "pages_written": written,
        "source_total_pages": total,
        "applied_offset": page_offset,
    }


@mcp.tool()
def preview_sections(
    pdf_path: str,
    page_range: str,
    section_titles: list[str] | None = None,
    page_offset: int = 0,
) -> dict[str, Any]:
    """Crop the PDF, convert to markdown, and detect sections — without
    writing any output files.

    Auto mode (default): detects numbered headings like "1.1.3. Title".
    Manual mode: when `section_titles` is provided, splits using those.

    Use this before `extract_sections` to verify what would be produced.
    """
    src = _expand(pdf_path)
    if not src.exists():
        raise FileNotFoundError(f"PDF not found: {src}")
    pages = _parse_page_range(page_range)
    if page_offset:
        pages = [p + page_offset for p in pages]

    tmp_pdf = Path("/tmp") / f"pdfsec_preview_{src.stem}.pdf"
    _split_pdf_to_file(src, pages, tmp_pdf)
    try:
        text = _pdf_to_markdown(tmp_pdf)
    finally:
        tmp_pdf.unlink(missing_ok=True)

    if section_titles:
        sections = _split_by_titles(text, section_titles)
        mode = "manual"
    else:
        sections = _auto_detect_numbered_sections(text)
        mode = "auto-numbered"

    preview = []
    for s in sections:
        if not s.get("title_only") and not s.get("content"):
            continue
        preview.append({
            "number": s.get("number"),
            "title": s["title_only"],
            "word_count": len(s["content"].split()),
            "char_count": len(s["content"]),
            "first_60_chars": s["content"][:60].replace("\n", " "),
        })

    not_found = []
    if sections and sections[0].get("_not_found"):
        not_found = list(sections[0]["_not_found"])

    return {
        "status": "ok",
        "mode": mode,
        "total_markdown_chars": len(text),
        "sections_detected": len(preview),
        "sections": preview,
        "titles_not_found": not_found,
    }


@mcp.tool()
async def extract_sections(
    ctx: Context,
    pdf_path: str,
    page_range: str,
    output_dir: str | None = None,
    labels: dict[str, Any] | None = None,
    parent_headers: dict[str, str] | None = None,
    section_titles: list[str] | None = None,
    page_offset: int = 0,
    save_raw_markdown: bool = True,
    save_split_pdf: bool = True,
    filename_prefix_with_number: bool = True,
    with_summaries: bool = True,
    summary_prompt: str | None = None,
    summary_max_tokens: int = 700,
) -> dict[str, Any]:
    """Full pipeline: crop the PDF → convert to markdown → detect sections →
    write one JSON file per section → optionally generate a summary for each.

    Parameters:
      pdf_path:    Source PDF.
      page_range:  Page range (e.g. "47-76").
      output_dir:  Output directory. Defaults to
                   `<Desktop>/<pdf-stem>-sections/` (Desktop resolved per OS,
                   including Windows OneDrive redirection).
      labels:      Free-form metadata copied into every JSON
                   (e.g. {"class": 10, "subject": "chemistry"}).
                   All keys are optional — nothing is required.
      parent_headers: Fixed parent headers (e.g.
                   {"header_1": "Interaction", "header_2": "Reactions"}).
                   The detected section title is appended at the next
                   available `header_N` slot.
      section_titles: Explicit list of section titles in order. When provided
                   the pipeline runs in manual mode; otherwise numbered
                   headings (1.1.3, 1.1.4, …) are auto-detected.
      page_offset: Added to every requested page (printed page vs PDF page).
      save_raw_markdown: Save the full markdown as `_full.md` (default True).
      save_split_pdf: Save the cropped PDF as `_split.pdf` (default True).
      filename_prefix_with_number: Prefix JSON filenames with the section
                   number (e.g. `1.1.3_mol_concept.json`).
      with_summaries: When True (default), request a summary for each section
                   from the MCP client's LLM via the sampling protocol and
                   write it into the `summarized_text` field. No external API
                   key is required — the summary is generated by whichever
                   model the client (Claude Code, Claude Desktop, etc.) is
                   already wired to. Set to False to skip and leave the field
                   empty.
      summary_prompt: Optional custom prompt template. Use the placeholder
                   `{content}` where the section body should be injected. The
                   default prompt asks for a 150–220 word standalone summary
                   in the source language.
      summary_max_tokens: Upper bound on tokens for each summary (default
                   700 — comfortably covers a 200-word paragraph).

    Returns the output directory, the list of created files, summary
    statistics, and a follow-up hint inviting the caller to run
    `validate_sections` for quality checks.
    """
    src = _expand(pdf_path)
    if not src.exists():
        raise FileNotFoundError(f"PDF not found: {src}")

    out_dir = (
        _expand(output_dir)
        if output_dir
        else _default_desktop() / f"{src.stem}-sections"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = _parse_page_range(page_range)
    if page_offset:
        pages = [p + page_offset for p in pages]

    # 1. Crop PDF
    split_pdf_path = out_dir / "_split.pdf"
    _split_pdf_to_file(src, pages, split_pdf_path)

    # 2. Convert to markdown
    text = _pdf_to_markdown(split_pdf_path)

    md_path = out_dir / "_full.md"
    if save_raw_markdown:
        md_path.write_text(text, encoding="utf-8")

    # 3. Detect / split sections
    titles_not_found: list[str] = []
    if section_titles:
        sections = _split_by_titles(text, section_titles)
        mode = "manual"
        if sections and sections[0].get("_not_found"):
            titles_not_found = list(sections[0]["_not_found"])
    else:
        sections = _auto_detect_numbered_sections(text)
        mode = "auto-numbered"

    # Drop any pseudo-section that exists only to carry _not_found
    sections = [s for s in sections if s.get("title_only") and s.get("content")]

    if not sections:
        return {
            "status": "no_sections_found",
            "output_dir": str(out_dir),
            "split_pdf": str(split_pdf_path) if save_split_pdf else None,
            "markdown": str(md_path) if save_raw_markdown else None,
            "hint": (
                "No sections detected. Inspect `_full.md` and call this tool "
                "again with an explicit `section_titles` list."
            ),
            "titles_not_found": titles_not_found,
        }

    # 4. Write JSON files
    created: list[dict[str, Any]] = []
    parent_headers = dict(parent_headers or {})
    labels = dict(labels or {})

    for s in sections:
        obj: dict[str, Any] = {}
        obj.update(labels)
        obj.update(parent_headers)

        next_idx = _next_header_index(obj)
        obj[f"header_{next_idx}"] = s["title_only"]

        obj["markdowned_text"] = s["content"]
        obj["summarized_text"] = ""

        slug = _slugify(s["title_only"])
        number = s.get("number")
        if filename_prefix_with_number and number:
            fname = f"{number}_{slug}.json"
        else:
            fname = f"{slug}.json"

        fpath = out_dir / fname
        fpath.write_text(
            json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        created.append({
            "file": str(fpath),
            "title": s["title_only"],
            "number": number,
            "word_count": len(s["content"].split()),
            "char_count": len(s["content"]),
        })

    # 5. Optionally generate summaries via the client's LLM (sampling)
    summary_stats = {
        "requested": bool(with_summaries),
        "succeeded": 0,
        "failed": 0,
        "errors": [],
    }
    if with_summaries and created:
        prompt_template = summary_prompt or DEFAULT_SUMMARY_PROMPT
        for record in created:
            fpath = Path(record["file"])
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
            except Exception as exc:
                summary_stats["failed"] += 1
                summary_stats["errors"].append({
                    "file": fpath.name, "error": f"json_read_failed: {exc}",
                })
                continue
            content = data.get("markdowned_text", "") or ""
            if not content.strip():
                summary_stats["failed"] += 1
                summary_stats["errors"].append({
                    "file": fpath.name, "error": "empty_markdowned_text",
                })
                continue
            summary, err = await _generate_summary(
                ctx, content, prompt_template, summary_max_tokens,
            )
            if err:
                summary_stats["failed"] += 1
                summary_stats["errors"].append({
                    "file": fpath.name, "error": err,
                })
                continue
            data["summarized_text"] = summary
            fpath.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            record["summary_word_count"] = len(summary.split())
            summary_stats["succeeded"] += 1

    if not save_split_pdf:
        split_pdf_path.unlink(missing_ok=True)

    return {
        "status": "ok",
        "mode": mode,
        "output_dir": str(out_dir),
        "split_pdf": str(split_pdf_path) if save_split_pdf else None,
        "markdown": str(md_path) if save_raw_markdown else None,
        "sections_created": created,
        "total_sections": len(created),
        "titles_not_found": titles_not_found,
        "summaries": summary_stats,
        "next_step_prompt": (
            "Would you like to validate the extracted sections to make sure "
            "no words are missing or characters are corrupted? Call "
            "`validate_sections` with this `output_dir` and pass "
            "`apply_fixes=true` to automatically repair any mismatches."
        ),
    }


@mcp.tool()
def validate_sections(
    output_dir: str,
    apply_fixes: bool = False,
    source_pdf: str | None = None,
    page_range: str | None = None,
    page_offset: int = 0,
) -> dict[str, Any]:
    """Validate JSON section files produced by `extract_sections`.

    The validator re-extracts the markdown from the cropped PDF (`_split.pdf`
    inside `output_dir`, or a fresh crop from `source_pdf` + `page_range`)
    and compares it to the `markdowned_text` stored in each JSON. It reports:

      * sections whose content appears truncated mid-sentence,
      * sections containing PDF extraction artifacts (replacement characters
        or `(cid:NN)` markers),
      * sections whose character count differs significantly from the fresh
        re-extraction (more than 5%),
      * JSON files with empty `markdowned_text`.

    Parameters:
      output_dir:  Directory containing the JSON files (and ideally a saved
                   `_split.pdf`).
      apply_fixes: When True, re-extracts the cropped PDF and overwrites each
                   JSON's `markdowned_text` from the fresh markdown using the
                   stored section title as anchor.
      source_pdf:  Optional. Re-crop the original PDF instead of relying on
                   the saved `_split.pdf`. Requires `page_range`.
      page_range:  Required if `source_pdf` is given.
      page_offset: Optional offset applied alongside `source_pdf` re-crop.

    Returns per-file diagnostics and, when `apply_fixes=True`, a list of
    files that were rewritten.
    """
    out_dir = _expand(output_dir)
    if not out_dir.exists():
        raise FileNotFoundError(f"output_dir not found: {out_dir}")

    # 1. Obtain a fresh markdown reference
    if source_pdf:
        if not page_range:
            raise ValueError("`page_range` is required when `source_pdf` is provided")
        src = _expand(source_pdf)
        if not src.exists():
            raise FileNotFoundError(f"source_pdf not found: {src}")
        pages = _parse_page_range(page_range)
        if page_offset:
            pages = [p + page_offset for p in pages]
        tmp_pdf = out_dir / "_split_validation.pdf"
        _split_pdf_to_file(src, pages, tmp_pdf)
        fresh_md = _pdf_to_markdown(tmp_pdf)
        tmp_pdf.unlink(missing_ok=True)
    else:
        split_pdf_path = out_dir / "_split.pdf"
        if not split_pdf_path.exists():
            raise FileNotFoundError(
                f"`_split.pdf` not found inside {out_dir}. Provide `source_pdf` "
                "and `page_range` so the validator can re-crop the source."
            )
        fresh_md = _pdf_to_markdown(split_pdf_path)

    # 2. Walk JSON files
    issues: list[dict[str, Any]] = []
    fixed: list[str] = []
    json_files = sorted(p for p in out_dir.glob("*.json"))

    for jf in json_files:
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as exc:
            issues.append({
                "file": jf.name,
                "error": f"JSON parse failed: {exc}",
            })
            continue

        # Find this section's title from the highest header_N
        title = None
        for key in sorted(data.keys()):
            if key.startswith("header_") and key.split("_")[-1].isdigit():
                title = data[key]
        if not title:
            issues.append({
                "file": jf.name,
                "error": "No header_N key found; cannot locate section in markdown.",
            })
            continue

        stored = data.get("markdowned_text", "") or ""

        # Locate the section in the fresh markdown
        anchor_idx = fresh_md.find(title)
        if anchor_idx < 0:
            issues.append({
                "file": jf.name,
                "title": title,
                "issue": "section_title_not_found_in_fresh_markdown",
                "hint": (
                    "The stored title is missing from the freshly extracted "
                    "markdown — the PDF cropping or title may have drifted."
                ),
            })
            continue

        # Slice the fresh content for this section: from this title up to the
        # next title we know about (using known JSON titles as boundaries).
        # We approximate by looking for the next known title in the same dir.
        other_titles = []
        for other in json_files:
            if other == jf:
                continue
            try:
                other_data = json.loads(other.read_text(encoding="utf-8"))
            except Exception:
                continue
            for key in sorted(other_data.keys()):
                if key.startswith("header_") and key.split("_")[-1].isdigit():
                    other_titles.append(other_data[key])

        end_idx = len(fresh_md)
        for ot in other_titles:
            pos = fresh_md.find(ot, anchor_idx + len(title))
            if pos > anchor_idx and pos < end_idx:
                end_idx = pos
        fresh_section = fresh_md[anchor_idx:end_idx].strip()

        # Diagnostics
        file_issues: list[str] = []
        if not stored.strip():
            file_issues.append("empty_markdowned_text")
        if _looks_truncated(stored):
            file_issues.append("looks_truncated_mid_sentence")
        if _has_replacement_chars(stored):
            file_issues.append("contains_pdf_extraction_artifacts")

        # Char-count drift
        if stored.strip() and fresh_section.strip():
            drift = abs(len(stored) - len(fresh_section)) / max(
                len(fresh_section), 1
            )
            if drift > 0.05:
                file_issues.append(
                    f"size_mismatch:{drift*100:.1f}%_drift"
                )

        if not file_issues:
            continue

        record: dict[str, Any] = {
            "file": jf.name,
            "title": title,
            "issues": file_issues,
            "stored_chars": len(stored),
            "fresh_chars": len(fresh_section),
        }

        if apply_fixes and fresh_section.strip():
            data["markdowned_text"] = fresh_section
            jf.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            fixed.append(jf.name)
            record["status"] = "fixed"

        issues.append(record)

    return {
        "status": "ok",
        "output_dir": str(out_dir),
        "files_scanned": len(json_files),
        "issues_found": issues,
        "fixed_files": fixed,
        "apply_fixes": apply_fixes,
        "hint": (
            "Re-run with `apply_fixes=true` to overwrite mismatched "
            "`markdowned_text` fields using the freshly extracted markdown."
            if issues and not apply_fixes
            else None
        ),
    }


@mcp.tool()
def detect_page_offset(
    pdf_path: str,
    book_page: int,
    expected_text: str,
    search_window: int = 10,
) -> dict[str, Any]:
    """Find the difference between the PDF physical page and the book's
    printed page number.

    Parameters:
      pdf_path:      Source PDF.
      book_page:     The printed page number you are targeting (e.g. 47).
      expected_text: A short snippet that should appear on that page (a
                     heading or the first few words).
      search_window: How many PDF pages around `book_page` to search
                     (default ±10).

    Returns matches with the suggested `page_offset` value to pass into
    `split_pdf` / `extract_sections`.
    """
    src = _expand(pdf_path)
    if not src.exists():
        raise FileNotFoundError(f"PDF not found: {src}")
    reader = PdfReader(str(src))
    total = len(reader.pages)
    needle = expected_text.strip()
    if not needle:
        raise ValueError("`expected_text` must not be empty")

    lo = max(1, book_page - search_window)
    hi = min(total, book_page + search_window)
    candidates = []
    for pdf_page in range(lo, hi + 1):
        try:
            page_text = reader.pages[pdf_page - 1].extract_text() or ""
        except Exception:
            page_text = ""
        if needle.lower() in page_text.lower():
            offset = pdf_page - book_page
            candidates.append({
                "pdf_page": pdf_page,
                "offset": offset,
                "preview": page_text[:160].replace("\n", " "),
            })

    return {
        "status": "ok" if candidates else "not_found",
        "book_page": book_page,
        "expected_text": needle,
        "search_window": search_window,
        "source_total_pages": total,
        "matches": candidates,
        "suggested_page_offset": (
            candidates[0]["offset"] if candidates else None
        ),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
