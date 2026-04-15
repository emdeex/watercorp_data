"""
extractor.py — Extract total operating expenditure and total water volume
from Victorian water corporation annual report PDFs.

Strategy waterfall (tried in order):
  1. Text search  — regex over raw text extracted by pdfplumber
  2. Table search — pdfplumber table extraction
  3. LLM fallback — claude-sonnet (only if use_llm=True and API key set)
"""

import io
import json
import logging
import os
import re
import tempfile

# pdfplumber is imported lazily inside functions to avoid Lambda cold-start
# failures when the native dependency chain (Pillow etc.) isn't available.

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword lists
# ---------------------------------------------------------------------------

OPEX_KEYWORDS = [
    "total expenses",
    "total expenditure",
    "operating expenses",
    "operating expenditure",
    "total operating expenditure",
    "total operating costs",
    "total operating expenses",
]

VOLUME_KEYWORDS = [
    "total water supplied",
    "water supplied to customers",
    "volume of water supplied",
    "total urban water supplied",
    "total volume of water supplied",
    "water delivered to customers",
    "total drinking water supplied",
]

VOLUME_EXCLUSIONS = [
    "recycled",
    "reclaimed",
    "bulk water purchase",
    "wholesale",
    "non-potable",
]

# Regex patterns for unit context
RE_THOUSANDS = re.compile(r"\$['\s]?000|\(thousands\)|\$000s", re.IGNORECASE)
RE_MILLIONS = re.compile(r"\$['\s]?m(?:illion)?s?\b|(?:\()?in millions(?:\))?", re.IGNORECASE)

# Regex to extract the first plausible dollar/number from a line or nearby text
RE_NUMBER = re.compile(r"([\d,]+(?:\.\d+)?)")

# Pages to skip (cover, contents, etc.)
SKIP_PAGES_BEFORE = 3


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract(pdf_bytes: bytes, corp_name: str, year: str, use_llm: bool = False) -> dict:
    """
    Extract opex (dollars) and volume (ML) from the PDF supplied as bytes.

    Returns a dict with keys:
        corporation, year,
        opex_dollars, volume_ml,
        opex_page, volume_page,
        extraction_method, confidence, notes, status
    """
    result = {
        "corporation": corp_name,
        "year": year,
        "opex_dollars": None,
        "volume_ml": None,
        "opex_page": None,
        "volume_page": None,
        "extraction_method": None,
        "confidence": "failed",
        "notes": "",
        "status": "ok",
    }

    try:
        import pdfplumber  # lazy import — keeps Flask startup fast on Vercel
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = pdf.pages[SKIP_PAGES_BEFORE:]

            # --- Strategy 1: text search ----------------------------------
            text_result = _text_strategy(pages)
            if text_result["opex_dollars"] and text_result["volume_ml"]:
                result.update(text_result)
                result["extraction_method"] = "text"
                result["confidence"] = "high"
                return result

            # Partial text hit — keep for blending
            partial = text_result

            # --- Strategy 2: table search ---------------------------------
            table_result = _table_strategy(pages)
            if table_result["opex_dollars"] and table_result["volume_ml"]:
                result.update(table_result)
                result["extraction_method"] = "table"
                result["confidence"] = "medium"
                return result

            # Blend partials
            blended = {
                "opex_dollars": partial.get("opex_dollars") or table_result.get("opex_dollars"),
                "volume_ml": partial.get("volume_ml") or table_result.get("volume_ml"),
                "opex_page": partial.get("opex_page") or table_result.get("opex_page"),
                "volume_page": partial.get("volume_page") or table_result.get("volume_page"),
            }
            if blended["opex_dollars"] and blended["volume_ml"]:
                result.update(blended)
                result["extraction_method"] = "blended"
                result["confidence"] = "medium"
                return result

            # --- Strategy 3: LLM ------------------------------------------
            if use_llm and os.environ.get("ANTHROPIC_API_KEY"):
                llm_result = _llm_strategy(pages, corp_name, year)
                if llm_result.get("opex_dollars") and llm_result.get("volume_ml"):
                    result.update(llm_result)
                    result["extraction_method"] = "llm"
                    result["confidence"] = "medium"
                    return result

            # All strategies failed
            result["notes"] = "Could not extract both values — manual review needed"
            result["confidence"] = "failed"
            if blended["opex_dollars"] or blended["volume_ml"]:
                result.update({k: v for k, v in blended.items() if v is not None})
                result["confidence"] = "low"
                result["extraction_method"] = "partial"

    except Exception as exc:
        logger.exception("Extraction error for %s %s: %s", corp_name, year, exc)
        result["status"] = "extract_failed"
        result["notes"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# Strategy 1 — Text
# ---------------------------------------------------------------------------

def _text_strategy(pages) -> dict:
    out = {
        "opex_dollars": None, "volume_ml": None,
        "opex_page": None, "volume_page": None,
    }

    for page in pages:
        text = page.extract_text() or ""
        if not text.strip():
            continue
        page_num = page.page_number

        if out["opex_dollars"] is None:
            val, unit_scale = _search_value(text, OPEX_KEYWORDS, exclusions=[])
            if val is not None:
                out["opex_dollars"] = int(val * unit_scale)
                out["opex_page"] = page_num

        if out["volume_ml"] is None:
            val, unit_scale = _search_value(text, VOLUME_KEYWORDS, exclusions=VOLUME_EXCLUSIONS)
            if val is not None:
                # Volume is typically reported in ML directly (no currency unit)
                out["volume_ml"] = int(val * unit_scale)
                out["volume_page"] = page_num

        if out["opex_dollars"] and out["volume_ml"]:
            break

    return out


def _search_value(text: str, keywords: list, exclusions: list):
    """
    Scan ``text`` for the first line matching one of ``keywords`` and
    return (raw_number, unit_scale) or (None, 1).
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        line_lower = line.lower()

        # Check any exclusion words
        if any(ex in line_lower for ex in exclusions):
            continue

        matched_keyword = next((kw for kw in keywords if kw in line_lower), None)
        if matched_keyword is None:
            continue

        # Context window: current line ± 2 lines
        ctx_start = max(0, i - 2)
        ctx_end = min(len(lines), i + 3)
        context = "\n".join(lines[ctx_start:ctx_end])

        unit_scale = _detect_unit_scale(context)
        number = _extract_number(line)
        if number is None:
            # Try the next line
            if i + 1 < len(lines):
                number = _extract_number(lines[i + 1])
        if number is not None and number > 0:
            return number, unit_scale

    return None, 1


def _detect_unit_scale(context: str) -> float:
    if RE_MILLIONS.search(context):
        return 1_000_000
    if RE_THOUSANDS.search(context):
        return 1_000
    return 1


def _extract_number(line: str) -> float | None:
    # Remove common formatting characters but keep digits, commas, dots
    cleaned = re.sub(r"[^\d,.\s]", " ", line)
    matches = RE_NUMBER.findall(cleaned)
    if not matches:
        return None
    for m in reversed(matches):  # rightmost number is usually the value
        try:
            val = float(m.replace(",", ""))
            if val > 0:
                return val
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Strategy 2 — Tables
# ---------------------------------------------------------------------------

def _table_strategy(pages) -> dict:
    out = {
        "opex_dollars": None, "volume_ml": None,
        "opex_page": None, "volume_page": None,
    }

    for page in pages:
        tables = page.extract_tables()
        if not tables:
            continue
        page_num = page.page_number

        for table in tables:
            for row in table:
                if not row:
                    continue
                row_text = " ".join(str(c or "").lower() for c in row)
                row_values = [str(c or "") for c in row]

                if out["opex_dollars"] is None:
                    if any(kw in row_text for kw in OPEX_KEYWORDS):
                        if not any(ex in row_text for ex in []):
                            val = _extract_rightmost_number(row_values)
                            if val:
                                # Tables usually report in $'000
                                out["opex_dollars"] = int(val * 1000)
                                out["opex_page"] = page_num

                if out["volume_ml"] is None:
                    if any(kw in row_text for kw in VOLUME_KEYWORDS):
                        if not any(ex in row_text for ex in VOLUME_EXCLUSIONS):
                            val = _extract_rightmost_number(row_values)
                            if val:
                                out["volume_ml"] = int(val)
                                out["volume_page"] = page_num

            if out["opex_dollars"] and out["volume_ml"]:
                return out

    return out


def _extract_rightmost_number(cells: list) -> float | None:
    for cell in reversed(cells):
        number = _extract_number(str(cell))
        if number and number > 0:
            return number
    return None


# ---------------------------------------------------------------------------
# Strategy 3 — LLM (Claude)
# ---------------------------------------------------------------------------

def _llm_strategy(pages, corp_name: str, year: str) -> dict:
    """
    Use Claude to extract values from the most relevant pages of the report.
    Only called when ANTHROPIC_API_KEY is set and use_llm=True.
    """
    import anthropic

    # Extract text from up to 30 pages, prioritising pages with financial content
    page_texts = []
    for page in pages:
        text = page.extract_text() or ""
        if len(text.strip()) > 100:
            page_texts.append((page.page_number, text))
        if len(page_texts) >= 30:
            break

    combined = "\n\n---PAGE BREAK---\n\n".join(
        f"[Page {pn}]\n{txt}" for pn, txt in page_texts
    )

    prompt = f"""You are a financial data extraction assistant. The text below is extracted from the {year} Annual Report of {corp_name}, a Victorian urban water utility.

Please extract:
1. **Total operating expenditure** — the total expenses/expenditure for the reporting entity for the financial year. This is typically in the Income Statement or Statement of Comprehensive Income. Report in DOLLARS (not $'000). Multiply by 1,000 if the report uses $'000.

2. **Total water volume supplied to customers** — the total volume of drinking/potable water supplied to customers for the year, in MEGALITRES (ML). This appears in a service performance table or operational section. Exclude recycled or reclaimed water volumes.

Return ONLY a JSON object with this exact structure:
{{
  "opex_dollars": <integer or null>,
  "volume_ml": <integer or null>,
  "opex_page": <integer or null>,
  "volume_page": <integer or null>,
  "notes": "<any relevant caveats or empty string>"
}}

If you cannot determine a value with reasonable confidence, use null.

---REPORT TEXT START---
{combined[:40000]}
---REPORT TEXT END---"""

    try:
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Extract JSON block if wrapped in markdown
        json_match = re.search(r"\{[\s\S]+\}", raw)
        if json_match:
            data = json.loads(json_match.group())
            return {
                "opex_dollars": data.get("opex_dollars"),
                "volume_ml": data.get("volume_ml"),
                "opex_page": data.get("opex_page"),
                "volume_page": data.get("volume_page"),
                "notes": data.get("notes", ""),
            }
    except Exception as exc:
        logger.warning("LLM extraction failed: %s", exc)

    return {}
