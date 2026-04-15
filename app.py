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
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# API — Config
# ---------------------------------------------------------------------------

@app.route("/api/config")
def api_config():
    """Return corporation list and year list for populating the UI."""
    corps = [
        {"name": c["name"], "slug": c["slug"], "is_gww_predecessor": c["is_gww_predecessor"]}
        for c in CORPORATIONS
    ]
    return jsonify({"corporations": corps, "years": YEARS, "base_year": BASE_YEAR})


# ---------------------------------------------------------------------------
# API — Process single report (scraper)
# ---------------------------------------------------------------------------

@app.route("/api/process-report", methods=["POST"])
def api_process_report():
    """
    Body: { corp_slug, year, use_llm }
    Returns result JSON for one report.
    Always returns HTTP 200; failures are signalled via the 'status' field.
    """
    body = request.get_json(force=True, silent=True) or {}
    slug = body.get("corp_slug", "")
    year = body.get("year", "")
    use_llm = bool(body.get("use_llm", False))

    corp = CORP_BY_SLUG.get(slug)
    if not corp:
        return jsonify({"status": "error", "error": f"Unknown corp slug: {slug}"}), 400
    if year not in YEARS:
        return jsonify({"status": "error", "error": f"Unknown year: {year}"}), 400

    # Download PDF
    try:
        pdf_bytes = scraper.find_and_download(corp, year)
    except Exception as exc:
        logger.exception("Scrape error %s %s", corp["name"], year)
        return jsonify({
            "corporation": corp["name"],
            "year": year,
            "status": "scrape_failed",
            "error": str(exc),
            "opex_dollars": None,
            "volume_ml": None,
            "extraction_method": None,
            "confidence": "failed",
            "notes": str(exc),
        })

    if pdf_bytes is None:
        return jsonify({
            "corporation": corp["name"],
            "year": year,
            "status": "scrape_failed",
            "error": "PDF not found on corporation website",
            "opex_dollars": None,
            "volume_ml": None,
            "extraction_method": None,
            "confidence": "failed",
            "notes": "PDF not found",
        })

    result = extract(pdf_bytes, corp["name"], year, use_llm=use_llm)
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — Process from direct URL (manual override)
# ---------------------------------------------------------------------------

@app.route("/api/process-url", methods=["POST"])
def api_process_url():
    """
    Body: { corp_slug, year, pdf_url, use_llm }
    Downloads the PDF from the given URL and extracts data.
    """
    body = request.get_json(force=True, silent=True) or {}
    slug = body.get("corp_slug", "")
    year = body.get("year", "")
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
        resp = req_lib.get(pdf_url, headers=HEADERS, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        pdf_bytes = resp.content
    except Exception as exc:
        return jsonify({
            "corporation": corp["name"],
            "year": year,
            "status": "scrape_failed",
            "error": f"Could not download URL: {exc}",
            "opex_dollars": None,
            "volume_ml": None,
            "extraction_method": None,
            "confidence": "failed",
            "notes": str(exc),
        })

    result = extract(pdf_bytes, corp["name"], year, use_llm=use_llm)
    result["notes"] = (result.get("notes") or "") + f" [manual URL]"
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — Process from uploaded file (manual override)
# ---------------------------------------------------------------------------

@app.route("/api/process-upload", methods=["POST"])
def api_process_upload():
    """
    Multipart form: corp_slug, year, use_llm, file (PDF).
    Extracts data from the uploaded PDF bytes.
    """
    slug = request.form.get("corp_slug", "")
    year = request.form.get("year", "")
    use_llm = request.form.get("use_llm", "false").lower() == "true"
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

    result = extract(pdf_bytes, corp["name"], year, use_llm=use_llm)
    result["notes"] = (result.get("notes") or "") + f" [uploaded: {uploaded.filename}]"
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — Calculate (CPI-adjust + build pivot)
# ---------------------------------------------------------------------------

@app.route("/api/calculate", methods=["POST"])
def api_calculate():
    """
    Body: { rows: [...raw extracted rows...] }
    Returns: { results, pivot }
    """
    body = request.get_json(force=True, silent=True) or {}
    rows = body.get("rows", [])

    if not rows:
        return jsonify({"error": "No rows provided"}), 400

    merged = merge_gww_predecessors(rows)
    results = calculate(merged, CPI_DATA, BASE_YEAR)
    pivot = build_pivot(results)

    return jsonify({"results": results, "pivot": pivot})


# ---------------------------------------------------------------------------
# API — CSV downloads (POST with data in body to stay stateless)
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
    merged = merge_gww_predecessors(rows)
    results = calculate(merged, CPI_DATA, BASE_YEAR)
    fieldnames = [
        "corporation", "year",
        "opex_dollars", "volume_ml",
        "cost_per_ml_nominal", "cost_per_ml_real",
        "cpi_index_used", "outlier_flag",
        "extraction_method", "confidence", "notes",
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
    csv_data = output.getvalue()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Vercel entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
