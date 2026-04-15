import os
import re
import time
import logging
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import TMP_DIR

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; WaterCorpDataExtractor/1.0; "
        "+https://github.com/emdeex/watercorp_data)"
    )
}

# URL fragments that indicate a PDF viewer rather than a direct download
VIEWER_PATTERNS = re.compile(
    r"(issuu\.com|publitas\.com|fliphtml5|yumpu\.com|calameo\.com)",
    re.IGNORECASE,
)


def _year_variants(year: str) -> list:
    """
    Return the set of substrings to look for in a URL or link text when
    matching a financial year like "2023-24".

    E.g. "2023-24" → ["2023-24", "2023_24", "202324", "2023", "23-24", "2324"]
    """
    m = re.match(r"(\d{4})-(\d{2})", year)
    if not m:
        return [year]
    full_start = m.group(1)         # "2023"
    short_end = m.group(2)          # "24"
    short_start = full_start[2:]    # "23"
    return [
        year,                                   # 2023-24
        year.replace("-", "_"),                 # 2023_24
        year.replace("-", ""),                  # 202324
        full_start,                             # 2023
        f"{short_start}-{short_end}",           # 23-24
        f"{short_start}{short_end}",            # 2324
    ]


class AnnualReportScraper:
    def __init__(self, timeout: int = 20, retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_and_download(self, corp: dict, year: str) -> bytes | None:
        """
        Attempt to find and download the annual report PDF for ``corp`` in
        ``year``.  Returns raw PDF bytes on success, None on failure.

        Caches to TMP_DIR/{slug}/{year}.pdf when the directory is writable.
        """
        slug = corp["slug"]
        cached = self._cache_path(slug, year)

        if cached and os.path.exists(cached):
            logger.info("Cache hit: %s", cached)
            with open(cached, "rb") as fh:
                return fh.read()

        pdf_url = self._locate_pdf(corp, year)
        if not pdf_url:
            logger.warning("Could not locate PDF for %s %s", corp["name"], year)
            return None

        logger.info("Downloading %s", pdf_url)
        pdf_bytes = self._download_pdf(pdf_url)
        if pdf_bytes and cached:
            self._write_cache(cached, pdf_bytes)
        return pdf_bytes

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _locate_pdf(self, corp: dict, year: str) -> str | None:
        """Try each report_pages path and return the first matching PDF URL."""
        base = corp["website"].rstrip("/")
        for path in corp.get("report_pages", []):
            page_url = base + path
            pdf_url = self._find_pdf_link(page_url, year)
            if pdf_url:
                return pdf_url
        return None

    def _find_pdf_link(self, page_url: str, year: str) -> str | None:
        """
        Fetch ``page_url`` and search for an ``<a>`` tag pointing to a PDF
        that matches the given financial year.
        """
        resp = self._get_with_retry(page_url)
        if resp is None:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        variants = _year_variants(year)

        candidates = []
        for tag in soup.find_all("a", href=True):
            href: str = tag["href"]
            if not href.lower().endswith(".pdf"):
                continue
            if VIEWER_PATTERNS.search(href):
                continue
            absolute = urljoin(page_url, href)
            link_text = tag.get_text(" ", strip=True).lower()
            href_lower = href.lower()
            # Match year in href path or link text
            for v in variants:
                if v in href_lower or v in link_text:
                    candidates.append(absolute)
                    break

        if candidates:
            return candidates[0]

        # Fallback: if only one PDF link on page, assume it's the right one
        all_pdfs = [
            urljoin(page_url, tag["href"])
            for tag in soup.find_all("a", href=True)
            if tag["href"].lower().endswith(".pdf")
            and not VIEWER_PATTERNS.search(tag["href"])
        ]
        if len(all_pdfs) == 1:
            return all_pdfs[0]

        return None

    def _download_pdf(self, url: str) -> bytes | None:
        """Download a PDF from ``url``, returning raw bytes or None."""
        resp = self._get_with_retry(url)
        if resp is None:
            return None
        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
            logger.warning(
                "Unexpected content-type '%s' for %s", content_type, url
            )
        return resp.content if resp.content else None

    def _get_with_retry(self, url: str) -> requests.Response | None:
        """GET with exponential backoff (2 s, 4 s, 8 s)."""
        delay = 2
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                logger.warning(
                    "Attempt %d/%d failed for %s: %s",
                    attempt, self.retries, url, exc,
                )
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
        return None

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_path(self, slug: str, year: str) -> str | None:
        """Return the cache file path, or None if TMP_DIR is not writable."""
        try:
            dir_path = os.path.join(TMP_DIR, slug)
            os.makedirs(dir_path, exist_ok=True)
            return os.path.join(dir_path, f"{year}.pdf")
        except OSError:
            return None

    def _write_cache(self, path: str, data: bytes) -> None:
        try:
            with open(path, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            logger.warning("Could not write cache %s: %s", path, exc)
