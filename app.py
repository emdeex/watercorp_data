"""
app.py — Flask application entry point for Vercel deployment.
"""

import io
import csv
import json
import logging
import os

from flask import Flask, jsonify, render_template, request, Response

from config import CORPORATIONS, YEARS, BASE_YEAR, CPI_DATA, CORP_BY_SLUG
from scraper import AnnualReportScraper
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
# API — Process single report
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

    # Extract data
    result = extract(pdf_bytes, corp["name"], year, use_llm=use_llm)
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

    # Merge GWW predecessors for pre-merger years
    merged = merge_gww_predecessors(rows)

    # CPI-adjust
    results = calculate(merged, CPI_DATA, BASE_YEAR)

    # Build pivot
    pivot = build_pivot(results)

    return jsonify({"results": results, "pivot": pivot})


# ---------------------------------------------------------------------------
# API — CSV downloads (POST with data in body to stay stateless)
# ---------------------------------------------------------------------------

@app.route("/api/download/raw", methods=["POST"])
def api_download_raw():
    """
    Body: { rows: [...] }
    Returns a CSV of the raw extracted data.
    """
    body = request.get_json(force=True, silent=True) or {}
    rows = body.get("rows", [])

    fieldnames = [
        "corporation", "year", "opex_dollars", "volume_ml",
        "opex_page", "volume_page", "extraction_method", "confidence", "notes", "status",
    ]
    return _rows_to_csv(rows, fieldnames, "watercorp_raw_data.csv")


@app.route("/api/download/results", methods=["POST"])
def api_download_results():
    """
    Body: { rows: [...raw rows...] }
    Runs calculate() and returns a CSV of the CPI-adjusted results.
    """
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

# Vercel looks for a callable named `app` in the module.
if __name__ == "__main__":
    app.run(debug=True)
