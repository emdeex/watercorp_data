"""
app.py — Flask application entry point for Vercel deployment.
"""

import io
import csv
import json
import logging
import os

import requests as req_lib
from flask import Flask, jsonify, render_template, request, Response

from config import CORPORATIONS, YEARS, BASE_YEAR, CPI_DATA, CORP_BY_SLUG
from scraper import AnnualReportScraper, HEADERS
from extractor import extract
from calculator import merge_gww_predecessors, calculate, build_pivot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
scraper = AnnualReportScraper()

# ---------------------------------------------------------------------------
# URL override helpers
#
# Two layers (session takes priority over bundle):
#   1. pdf_url_overrides.json  — committed to repo; survives cold starts
#   2. /tmp/pdf_url_overrides.json — written via config UI; ephemeral
# ---------------------------------------------------------------------------

_BUNDLE_OVERRIDES = os.path.join(os.path.dirname(__file__), "pdf_url_overrides.json")
_SESSION_OVERRIDES = "/tmp/pdf_url_overrides.json"


def load_overrides() -> dict:
    out = {}
    for path in (_BUNDLE_OVERRIDES, _SESSION_OVERRIDES):
        try:
            with open(path) as f:
                out.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    return out


def save_overrides(data: dict) -> None:
    """Persist to session file (/tmp). Also rewrites bundle file if writable."""
    os.makedirs("/tmp", exist_ok=True)
    with open(_SESSION_OVERRIDES, "w") as f:
        json.dump(data, f, indent=2)
    # Also try to update the bundle file so a redeploy picks it up
    try:
        with open(_BUNDLE_OVERRIDES, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass  # read-only in Lambda; that's fine


def override_key(slug: str, year: str) -> str:
    return f"{slug}/{year}"


def cached_pdf_path(slug: str, year: str) -> str | None:
    """Return /tmp cache path for a PDF, or None if /tmp is not writable."""
    from config import TMP_DIR
    try:
        d = os.path.join(TMP_DIR, slug)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{year}.pdf")
    except OSError:
        return None


def download_pdf_bytes(url: str) -> bytes:
    """Download a PDF from url; raises on HTTP error."""
    resp = req_lib.get(url, headers=HEADERS, timeout=60, allow_redirects=True)
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def config_page():
    return render_template("config.html")


# ---------------------------------------------------------------------------
# API — App config (corps + years)
# ---------------------------------------------------------------------------

@app.route("/api/config")
def api_config():
    corps = [
        {"name": c["name"], "slug": c["slug"], "is_gww_predecessor": c["is_gww_predecessor"]}
        for c in CORPORATIONS
    ]
    return jsonify({"corporations": corps, "years": YEARS, "base_year": BASE_YEAR})


# ---------------------------------------------------------------------------
# API — URL override CRUD
# ---------------------------------------------------------------------------

@app.route("/api/overrides", methods=["GET"])
def api_get_overrides():
    """Return all configured URL overrides plus cache status for each."""
    overrides = load_overrides()
    result = []
    for key, url in overrides.items():
        parts = key.split("/", 1)
        slug, year = (parts[0], parts[1]) if len(parts) == 2 else (key, "")
        corp = CORP_BY_SLUG.get(slug)
        cache_path = cached_pdf_path(slug, year)
        cached = bool(cache_path and os.path.exists(cache_path))
        result.append({
            "key": key,
            "slug": slug,
            "year": year,
            "corp_name": corp["name"] if corp else slug,
            "url": url,
            "cached": cached,
        })
    result.sort(key=lambda r: (r["corp_name"], r["year"]))
    return jsonify(result)


@app.route("/api/overrides", methods=["POST"])
def api_save_override():
    """
    Body: { slug, year, url }
    Saves one override entry. Pass url="" to delete.
    """
    body = request.get_json(force=True, silent=True) or {}
    slug = body.get("slug", "").strip()
    year = body.get("year", "").strip()
    url  = body.get("url", "").strip()

    if not slug or not year:
        return jsonify({"error": "slug and year are required"}), 400

    overrides = load_overrides()
    key = override_key(slug, year)
    if url:
        overrides[key] = url
    else:
        overrides.pop(key, None)

    save_overrides(overrides)
    return jsonify({"ok": True, "key": key, "url": url})


@app.route("/api/overrides/import", methods=["POST"])
def api_import_overrides():
    """Body: { overrides: {key: url, ...} } — replace all overrides."""
    body = request.get_json(force=True, silent=True) or {}
    data = body.get("overrides", {})
    if not isinstance(data, dict):
        return jsonify({"error": "overrides must be an object"}), 400
    # Strip empty values
    data = {k: v for k, v in data.items() if v and str(v).strip()}
    save_overrides(data)
    return jsonify({"ok": True, "count": len(data)})


@app.route("/api/overrides/fetch", methods=["POST"])
def api_fetch_override():
    """
    Body: { slug, year }
    Downloads the PDF from the configured override URL and caches it to /tmp.
    """
    body = request.get_json(force=True, silent=True) or {}
    slug = body.get("slug", "").strip()
    year = body.get("year", "").strip()

    overrides = load_overrides()
    key = override_key(slug, year)
    url = overrides.get(key)
    if not url:
        return jsonify({"error": "No override URL configured for this entry"}), 400

    corp = CORP_BY_SLUG.get(slug)
    corp_name = corp["name"] if corp else slug

    try:
        pdf_bytes = download_pdf_bytes(url)
    except Exception as exc:
        return jsonify({"error": f"Download failed: {exc}"}), 502

    cache_path = cached_pdf_path(slug, year)
    if cache_path:
        with open(cache_path, "wb") as f:
            f.write(pdf_bytes)

    return jsonify({
        "ok": True,
        "slug": slug,
        "year": year,
        "corp_name": corp_name,
        "size_kb": round(len(pdf_bytes) / 1024),
        "cached": bool(cache_path),
        "cache_path": cache_path,
    })


# ---------------------------------------------------------------------------
# API — Process single report (scraper, with override check)
# ---------------------------------------------------------------------------

@app.route("/api/process-report", methods=["POST"])
def api_process_report():
    body = request.get_json(force=True, silent=True) or {}
    slug     = body.get("corp_slug", "")
    year     = body.get("year", "")
    use_llm  = bool(body.get("use_llm", False))

    corp = CORP_BY_SLUG.get(slug)
    if not corp:
        return jsonify({"status": "error", "error": f"Unknown corp slug: {slug}"}), 400
    if year not in YEARS:
        return jsonify({"status": "error", "error": f"Unknown year: {year}"}), 400

    pdf_bytes = None
    source_note = ""

    # 1. Check /tmp cache first
    cache_path = cached_pdf_path(slug, year)
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            pdf_bytes = f.read()
        source_note = "[cached]"
        logger.info("Using cached PDF for %s %s", corp["name"], year)

    # 2. Check URL override
    if pdf_bytes is None:
        overrides = load_overrides()
        key = override_key(slug, year)
        if key in overrides:
            try:
                pdf_bytes = download_pdf_bytes(overrides[key])
                source_note = "[override URL]"
                if cache_path:
                    with open(cache_path, "wb") as f:
                        f.write(pdf_bytes)
            except Exception as exc:
                return jsonify({
                    "corporation": corp["name"], "year": year,
                    "status": "scrape_failed",
                    "error": f"Override URL download failed: {exc}",
                    "opex_dollars": None, "volume_ml": None,
                    "extraction_method": None, "confidence": "failed",
                    "notes": str(exc),
                })

    # 3. Scrape
    if pdf_bytes is None:
        try:
            pdf_bytes = scraper.find_and_download(corp, year)
            source_note = "[scraped]"
        except Exception as exc:
            logger.exception("Scrape error %s %s", corp["name"], year)
            return jsonify({
                "corporation": corp["name"], "year": year,
                "status": "scrape_failed", "error": str(exc),
                "opex_dollars": None, "volume_ml": None,
                "extraction_method": None, "confidence": "failed",
                "notes": str(exc),
            })

    if pdf_bytes is None:
        return jsonify({
            "corporation": corp["name"], "year": year,
            "status": "scrape_failed",
            "error": "PDF not found — set a URL override in the Config page",
            "opex_dollars": None, "volume_ml": None,
            "extraction_method": None, "confidence": "failed",
            "notes": "PDF not found",
        })

    result = extract(pdf_bytes, corp["name"], year, use_llm=use_llm)
    if source_note:
        result["notes"] = (result.get("notes") or "") + " " + source_note
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — Process from direct URL (manual override)
# ---------------------------------------------------------------------------

@app.route("/api/process-url", methods=["POST"])
def api_process_url():
    body    = request.get_json(force=True, silent=True) or {}
    slug    = body.get("corp_slug", "")
    year    = body.get("year", "")
    pdf_url = (body.get("pdf_url") or "").strip()
    use_llm = bool(body.get("use_llm", False))

    corp = CORP_BY_SLUG.get(slug)
    if not corp:
        return jsonify({"status": "error", "error": f"Unknown corp slug: {slug}"}), 400
    if year not in YEARS:
        return jsonify({"status": "error", "error": f"Unknown year: {year}"}), 400
    if not pdf_url:
        return jsonify({"status": "error", "error": "pdf_url is required"}), 400

    try:
        pdf_bytes = download_pdf_bytes(pdf_url)
    except Exception as exc:
        return jsonify({
            "corporation": corp["name"], "year": year,
            "status": "scrape_failed", "error": f"Could not download URL: {exc}",
            "opex_dollars": None, "volume_ml": None,
            "extraction_method": None, "confidence": "failed", "notes": str(exc),
        })

    cache_path = cached_pdf_path(slug, year)
    if cache_path:
        with open(cache_path, "wb") as f:
            f.write(pdf_bytes)

    result = extract(pdf_bytes, corp["name"], year, use_llm=use_llm)
    result["notes"] = (result.get("notes") or "") + " [manual URL]"
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — Process from uploaded file (manual override)
# ---------------------------------------------------------------------------

@app.route("/api/process-upload", methods=["POST"])
def api_process_upload():
    slug     = request.form.get("corp_slug", "")
    year     = request.form.get("year", "")
    use_llm  = request.form.get("use_llm", "false").lower() == "true"
    uploaded = request.files.get("file")

    corp = CORP_BY_SLUG.get(slug)
    if not corp:
        return jsonify({"status": "error", "error": f"Unknown corp slug: {slug}"}), 400
    if year not in YEARS:
        return jsonify({"status": "error", "error": f"Unknown year: {year}"}), 400
    if not uploaded:
        return jsonify({"status": "error", "error": "No file uploaded"}), 400

    pdf_bytes = uploaded.read()
    if not pdf_bytes:
        return jsonify({"status": "error", "error": "Uploaded file is empty"}), 400

    cache_path = cached_pdf_path(slug, year)
    if cache_path:
        with open(cache_path, "wb") as f:
            f.write(pdf_bytes)

    result = extract(pdf_bytes, corp["name"], year, use_llm=use_llm)
    result["notes"] = (result.get("notes") or "") + f" [uploaded: {uploaded.filename}]"
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — Calculate
# ---------------------------------------------------------------------------

@app.route("/api/calculate", methods=["POST"])
def api_calculate():
    body = request.get_json(force=True, silent=True) or {}
    rows = body.get("rows", [])
    if not rows:
        return jsonify({"error": "No rows provided"}), 400
    merged  = merge_gww_predecessors(rows)
    results = calculate(merged, CPI_DATA, BASE_YEAR)
    pivot   = build_pivot(results)
    return jsonify({"results": results, "pivot": pivot})


# ---------------------------------------------------------------------------
# API — CSV downloads
# ---------------------------------------------------------------------------

@app.route("/api/download/raw", methods=["POST"])
def api_download_raw():
    body = request.get_json(force=True, silent=True) or {}
    rows = body.get("rows", [])
    fieldnames = [
        "corporation", "year", "opex_dollars", "volume_ml",
        "opex_page", "volume_page", "extraction_method", "confidence", "notes", "status",
    ]
    return _rows_to_csv(rows, fieldnames, "watercorp_raw_data.csv")


@app.route("/api/download/results", methods=["POST"])
def api_download_results():
    body = request.get_json(force=True, silent=True) or {}
    rows = body.get("rows", [])
    merged  = merge_gww_predecessors(rows)
    results = calculate(merged, CPI_DATA, BASE_YEAR)
    fieldnames = [
        "corporation", "year", "opex_dollars", "volume_ml",
        "cost_per_ml_nominal", "cost_per_ml_real",
        "cpi_index_used", "outlier_flag", "extraction_method", "confidence", "notes",
    ]
    return _rows_to_csv(results, fieldnames, "watercorp_results.csv")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows_to_csv(rows: list, fieldnames: list, filename: str) -> Response:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    app.run(debug=True)
