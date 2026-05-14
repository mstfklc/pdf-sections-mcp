# PDF Sections MCP

A Model Context Protocol server that turns a page range of any PDF into one
clean JSON file per section. It handles the cropping, the markdown
conversion, the section detection, and the file writing — and offers a
validation pass at the end so you can catch missing words or extraction
artifacts before shipping the data anywhere.

> Designed for textbooks with numbered sections (1.1.3, 1.1.4, …) but works
> on any PDF when you provide section titles explicitly.

---

## What it does

```
 PDF + page range
        │
        ▼
   ┌─────────────────────────────────────────────────────┐
   │ 1. Crop the PDF to the requested pages              │
   │ 2. Convert the crop to Markdown (via markitdown)    │
   │ 3. Detect section boundaries                        │
   │      • auto-detect numbered headings, OR            │
   │      • split on a caller-supplied list of titles    │
   │ 4. Write one JSON per section + raw markdown + crop │
   │ 5. (optional) Validate against a fresh extraction   │
   └─────────────────────────────────────────────────────┘
        │
        ▼
 ~/Desktop/<pdf-stem>-sections/
   ├── _split.pdf
   ├── _full.md
   ├── 1.1.3_mol_concept.json
   ├── 1.1.4_balancing_equations.json
   └── 1.1.5_stoichiometric_calculations.json
```

Each generated JSON looks like this (the schema is fully user-defined —
nothing is hard-coded):

```json
{
  "class": 10,
  "subject": "chemistry",
  "header_1": "Interaction",
  "header_2": "Chemical Reactions",
  "header_3": "Mol Concept",
  "markdowned_text": "## Mol Concept\n\nUnits of measurement…",
  "summarized_text": ""
}
```

---

## Installation

### 1. Install `uv`

The server is a single self-contained Python script with inline dependency
metadata (PEP 723). `uv` resolves and runs it without any virtualenv setup.

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Verify:

```bash
uv --version
```

### 2. Drop `server.py` somewhere stable

The recommended location is `~/Desktop/pdf-sections-mcp/server.py`, but any
absolute path works. The first run of `uv run --script server.py` will
download Python (if needed) and install `mcp`, `pypdf`, and `markitdown`.

### 3. Register the MCP

You have two options: install directly from GitHub (no clone required) or
from a local copy.

#### Option A — Install straight from GitHub (recommended)

`uv` can execute a script that lives at a URL. Replace `<owner>/<repo>` with
this repository's path on GitHub.

**Claude Code (CLI):**

```bash
claude mcp add pdf-sections -- \
  uv run --script \
  https://raw.githubusercontent.com/<owner>/<repo>/main/server.py
```

**Claude Desktop (manual config):** edit
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pdf-sections": {
      "command": "uv",
      "args": [
        "run",
        "--script",
        "https://raw.githubusercontent.com/<owner>/<repo>/main/server.py"
      ]
    }
  }
}
```

The first run downloads `server.py`, resolves the PEP 723 dependency block,
and caches everything locally. Subsequent runs are fast.

#### Option B — Install from a local copy

```bash
git clone https://github.com/<owner>/<repo>.git ~/pdf-sections-mcp
claude mcp add pdf-sections -- \
  uv run --script ~/pdf-sections-mcp/server.py
```

Or with the Claude Desktop config pointing to the local path:

```json
{
  "mcpServers": {
    "pdf-sections": {
      "command": "uv",
      "args": [
        "run",
        "--script",
        "/Users/<you>/pdf-sections-mcp/server.py"
      ]
    }
  }
}
```

Restart Claude. The `pdf-sections` server should appear with five tools:
`split_pdf`, `preview_sections`, `extract_sections`, `validate_sections`,
`detect_page_offset`.

---

## Tools

| Tool | Purpose |
|---|---|
| `split_pdf` | Crop only — produces a new PDF, no markdown work |
| `preview_sections` | Detect sections **without** writing files |
| `extract_sections` | The main one-shot pipeline |
| `validate_sections` | Verify and repair existing section JSONs |
| `detect_page_offset` | Find the offset between book pages and PDF pages |

### Full parameter reference

| Parameter | Default | Notes |
|---|---|---|
| `pdf_path` | — | Source PDF. Accepts `~/...` or absolute paths. |
| `page_range` | — | `"47-76"`, `"5,7,9-12"`, `"1-3,5,8"`. |
| `output_path` | `~/Desktop/<stem>-<range>.pdf` | `split_pdf` only. |
| `output_dir` | `~/Desktop/<stem>-sections/` | `extract_sections`, `validate_sections`. |
| `page_offset` | `0` | Added to every page (book page → PDF page). |
| `labels` | `{}` | Free-form metadata copied into every JSON. Keys are arbitrary. |
| `parent_headers` | `{}` | Fixed `header_N` values inherited by all sections. |
| `section_titles` | `null` | Optional ordered list of titles for manual splitting. |
| `save_raw_markdown` | `true` | Save `_full.md` next to the JSONs. |
| `save_split_pdf` | `true` | Save `_split.pdf` next to the JSONs. |
| `filename_prefix_with_number` | `true` | Names files `1.1.3_title.json`. |
| `apply_fixes` | `false` | `validate_sections` only — rewrite mismatched files. |

---

## Usage examples

The examples below use a fictional chemistry textbook (`chemistry-10.pdf`)
where book page 47 starts the section "1.1.3 Mol Concept". Replace paths and
titles with whatever your own PDF needs.

### Example 1 — Locate the page offset first

PDFs often have a few introductory pages before the printed page numbering
starts. Use `detect_page_offset` once to learn the offset for the whole book.

```json
{
  "tool": "detect_page_offset",
  "arguments": {
    "pdf_path": "~/Downloads/chemistry-10.pdf",
    "book_page": 47,
    "expected_text": "Mol Concept"
  }
}
```

Typical response:

```json
{
  "status": "ok",
  "matches": [
    { "pdf_page": 48, "offset": 1, "preview": "47 1. TOPIC / Interaction Mol Concept ..." }
  ],
  "suggested_page_offset": 1
}
```

Remember the `suggested_page_offset` (`1` in this case) and pass it to every
later call.

### Example 2 — Preview before writing anything

```json
{
  "tool": "preview_sections",
  "arguments": {
    "pdf_path": "~/Downloads/chemistry-10.pdf",
    "page_range": "47-76",
    "page_offset": 1
  }
}
```

Response shows the detected boundaries — verify the titles, word counts,
and the first sixty characters of each section before committing.

```json
{
  "status": "ok",
  "mode": "auto-numbered",
  "sections_detected": 3,
  "sections": [
    { "number": "1.1.3", "title": "Mol Concept", "word_count": 1900, "first_60_chars": "Units of measurement make daily life more tangible …" },
    { "number": "1.1.4", "title": "Balancing Chemical Equations", "word_count": 1850, "first_60_chars": "The law of conservation of mass was first proposed …" },
    { "number": "1.1.5", "title": "Stoichiometric Calculations", "word_count": 1720, "first_60_chars": "In chemistry as in many fields, equations are used …" }
  ]
}
```

### Example 3 — Extract sections (auto-detected headings)

```json
{
  "tool": "extract_sections",
  "arguments": {
    "pdf_path": "~/Downloads/chemistry-10.pdf",
    "page_range": "47-76",
    "page_offset": 1,
    "labels": { "class": 10, "subject": "chemistry" },
    "parent_headers": {
      "header_1": "Interaction",
      "header_2": "Chemical Reactions"
    }
  }
}
```

Output:

```
~/Desktop/chemistry-10-sections/
├── _split.pdf
├── _full.md
├── 1.1.3_mol_concept.json
├── 1.1.4_balancing_chemical_equations.json
└── 1.1.5_stoichiometric_calculations.json
```

Each JSON file:

```json
{
  "class": 10,
  "subject": "chemistry",
  "header_1": "Interaction",
  "header_2": "Chemical Reactions",
  "header_3": "Mol Concept",
  "markdowned_text": "## Mol Concept\n\n…",
  "summarized_text": ""
}
```

The response from the tool always ends with a follow-up question:

```text
Would you like to validate the extracted sections to make sure no words
are missing or characters are corrupted? Call `validate_sections` with this
`output_dir` and pass `apply_fixes=true` to automatically repair any
mismatches.
```

Confirm with the user before deciding whether to run validation.

### Example 4 — Extract sections (manual title list)

When a section lacks a numbered prefix (e.g. an interior sub-heading like
"Measurable Properties of Pure Substances"), pass an explicit ordered list
of titles. The pipeline will split using substring matches.

```json
{
  "tool": "extract_sections",
  "arguments": {
    "pdf_path": "~/Downloads/chemistry-10.pdf",
    "page_range": "47-60",
    "page_offset": 1,
    "section_titles": [
      "1.1.3. Mol Concept",
      "Measurable Properties of Pure Substances"
    ],
    "labels": { "class": 10, "subject": "chemistry" },
    "parent_headers": {
      "header_1": "Interaction",
      "header_2": "Chemical Reactions",
      "header_3": "Mol Concept"
    }
  }
}
```

If a title is not found in the markdown, the response surfaces it under
`titles_not_found` so you can fix the typo (curly quotes, accented
characters, hidden whitespace) and rerun.

### Example 5 — Validate after extraction

After `extract_sections` writes the JSON files, ask the user whether to
validate. If yes:

```json
{
  "tool": "validate_sections",
  "arguments": {
    "output_dir": "~/Desktop/chemistry-10-sections",
    "apply_fixes": true
  }
}
```

What validation checks:

- **Empty body** — `markdowned_text` is blank.
- **Truncated content** — the body ends mid-sentence (heuristic on last
  character).
- **Encoding artifacts** — replacement characters (`�`) or `(cid:NN)` markers
  from broken font mappings.
- **Size drift** — the stored body is more than 5% larger or smaller than a
  freshly re-extracted copy of the same section.

Response shape:

```json
{
  "status": "ok",
  "files_scanned": 3,
  "issues_found": [
    {
      "file": "1.1.4_balancing_chemical_equations.json",
      "title": "Balancing Chemical Equations",
      "issues": ["size_mismatch:7.2%_drift"],
      "stored_chars": 7400,
      "fresh_chars": 7960,
      "status": "fixed"
    }
  ],
  "fixed_files": ["1.1.4_balancing_chemical_equations.json"]
}
```

With `apply_fixes=true`, the validator overwrites the stale
`markdowned_text` fields using the freshly extracted markdown, preserving
all other JSON keys (labels, headers, summaries).

When `apply_fixes=false` it only reports issues, so you can review before
modifying anything.

### Example 6 — Crop only (no markdown work)

For situations where you just want a smaller PDF:

```json
{
  "tool": "split_pdf",
  "arguments": {
    "pdf_path": "~/Downloads/chemistry-10.pdf",
    "page_range": "47-76",
    "output_path": "~/Desktop/chemistry-mol-stoichio.pdf",
    "page_offset": 1
  }
}
```

---

## Recommended workflow

Walking through every tool in order:

1. **`detect_page_offset`** — confirm the gap between the printed page and
   the PDF physical page.
2. **`preview_sections`** — verify the auto-detection covers all the headings
   you expect. If anything is missing, prepare a `section_titles` list.
3. **`extract_sections`** — write the JSON files. Read the response's
   follow-up prompt aloud and **ask the user whether to validate**.
4. **`validate_sections`** with `apply_fixes=false` — show the user the
   detected issues.
5. **`validate_sections`** with `apply_fixes=true` — only if the user
   approves the repair.

---

## Notes & caveats

- The `summarized_text` field is intentionally left empty. Summaries depend
  on a language model — produce them in your client (Claude, etc.) after the
  JSONs are written.
- `markitdown` is a high-quality but somewhat opinionated extractor. Some
  PDFs interleave running headers (page numbers, chapter titles) with body
  text; this can show up inside section bodies. Use `validate_sections` to
  catch noticeable drift.
- The auto-detection heuristic looks for headings shaped like
  `^\d+(?:\.\d+){1,5}\.?\s+Title`. Unnumbered sub-headings will **not** be
  found automatically — provide `section_titles` for those.
- Page numbers are 1-indexed end-to-end. Page ranges are inclusive.
- All output paths default to `~/Desktop/`. Override with `output_dir` or
  `output_path` if you prefer somewhere else.
- The `_split.pdf` saved next to the JSONs is what `validate_sections` uses
  to re-extract the reference markdown. Keep it around if you intend to
  validate later.

---

## Troubleshooting

**"No sections detected"** — the auto-detection regex did not find numbered
headings. Inspect `_full.md` (saved in the output dir) and pass an explicit
`section_titles` list.

**"section_title_not_found_in_fresh_markdown"** during validation — the
title stored in the JSON does not match the markdown anymore. This usually
means the title was edited by hand. Restore the original title or rerun
`extract_sections`.

**Issue: words missing from a section** — Run `validate_sections` with
`apply_fixes=true`. The fresh markdown extraction replaces the stored body
in-place while keeping all other JSON keys.

**Issue: replacement characters (`�`)** — the source PDF uses a non-standard
font encoding. Re-export the PDF from the original source if possible, or
provide a cleaner copy.

---

## File layout

```
~/Desktop/pdf-sections-mcp/
├── server.py         # The entire MCP — single file, PEP 723 metadata
└── README.md         # This document
```
