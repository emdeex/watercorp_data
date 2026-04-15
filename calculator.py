from config import CPI_DATA, BASE_YEAR, GWW_MERGER_YEAR, OUTLIER_LOW, OUTLIER_HIGH, YEARS


def merge_gww_predecessors(rows: list) -> list:
    """
    Before GWW_MERGER_YEAR, City West Water and Western Water were separate.
    For years where both predecessors have data, sum their opex and volume and
    relabel the combined row as "Greater Western Water".

    For years >= GWW_MERGER_YEAR the GWW row passes through unchanged.
    Predecessor rows for years before the merger are removed after merging.
    """
    merger_index = YEARS.index(GWW_MERGER_YEAR)
    pre_merger_years = set(YEARS[:merger_index])

    predecessor_names = {"City West Water", "Western Water"}
    buckets: dict = {}  # year -> {opex, volume, count, notes}
    passthrough = []

    for row in rows:
        corp = row.get("corporation", "")
        year = row.get("year", "")
        if corp in predecessor_names and year in pre_merger_years:
            if year not in buckets:
                buckets[year] = {
                    "corporation": "Greater Western Water",
                    "year": year,
                    "opex_dollars": 0,
                    "volume_ml": 0,
                    "count": 0,
                    "notes": [],
                    "extraction_method": "merged",
                    "confidence": "high",
                    "status": "ok",
                    "opex_page": None,
                    "volume_page": None,
                }
            b = buckets[year]
            if row.get("opex_dollars") is not None:
                b["opex_dollars"] += row["opex_dollars"]
                b["count"] += 1
            if row.get("volume_ml") is not None:
                b["volume_ml"] += row["volume_ml"]
            if row.get("notes"):
                b["notes"].append(f"{corp}: {row['notes']}")
            # Downgrade confidence if any predecessor is not high
            if row.get("confidence") != "high":
                b["confidence"] = "medium"
        else:
            passthrough.append(row)

    merged = []
    for year, b in buckets.items():
        b["notes"] = "; ".join(b["notes"]) if b["notes"] else ""
        # Only include if both predecessors contributed
        if b["count"] >= 2 and b["volume_ml"] > 0:
            merged.append(b)

    return passthrough + merged


def calculate(rows: list, cpi_data: dict = None, base_year: str = None) -> list:
    """
    For each row with valid opex_dollars and volume_ml:
      - compute nominal cost per ML
      - apply CPI deflation to base_year dollars
      - flag outliers
    Rows with status != 'ok' or missing values are passed through with None costs.
    """
    if cpi_data is None:
        cpi_data = CPI_DATA
    if base_year is None:
        base_year = BASE_YEAR

    base_cpi = cpi_data.get(base_year, 1.0)
    results = []

    for row in rows:
        out = dict(row)
        opex = row.get("opex_dollars")
        vol = row.get("volume_ml")
        year = row.get("year", "")

        if row.get("status") == "ok" and opex and vol and vol > 0:
            year_cpi = cpi_data.get(year, base_cpi)
            nominal = opex / vol
            real = nominal * (base_cpi / year_cpi)
            out["cost_per_ml_nominal"] = round(nominal, 2)
            out["cost_per_ml_real"] = round(real, 2)
            out["cpi_index_used"] = year_cpi
            out["outlier_flag"] = not (OUTLIER_LOW <= real <= OUTLIER_HIGH)
        else:
            out["cost_per_ml_nominal"] = None
            out["cost_per_ml_real"] = None
            out["cpi_index_used"] = cpi_data.get(year)
            out["outlier_flag"] = False

        results.append(out)

    return results


def build_pivot(results: list) -> dict:
    """
    Build a pivot structure: {corps, years, values} where
    values[i][j] is the real $/ML for corps[i] in years[j], or None.
    Also returns outlier_flags[i][j].
    """
    corps_seen: dict = {}  # name -> index
    years_seen: dict = {}  # year -> index (ordered by YEARS)

    # Collect unique corps and years in canonical order
    for row in results:
        y = row.get("year", "")
        if y not in years_seen and y in YEARS:
            years_seen[y] = YEARS.index(y)

    sorted_years = sorted(years_seen.keys(), key=lambda y: years_seen[y])

    for row in results:
        corp = row.get("corporation", "")
        if corp and corp not in corps_seen:
            corps_seen[corp] = len(corps_seen)

    sorted_corps = sorted(corps_seen.keys(), key=lambda c: corps_seen[c])

    # Build 2-D grids
    n_corps = len(sorted_corps)
    n_years = len(sorted_years)
    values = [[None] * n_years for _ in range(n_corps)]
    outliers = [[False] * n_years for _ in range(n_corps)]

    year_idx = {y: i for i, y in enumerate(sorted_years)}
    corp_idx = {c: i for i, c in enumerate(sorted_corps)}

    for row in results:
        corp = row.get("corporation", "")
        year = row.get("year", "")
        if corp in corp_idx and year in year_idx:
            ci = corp_idx[corp]
            yi = year_idx[year]
            values[ci][yi] = row.get("cost_per_ml_real")
            outliers[ci][yi] = row.get("outlier_flag", False)

    return {
        "corps": sorted_corps,
        "years": sorted_years,
        "values": values,
        "outliers": outliers,
    }
