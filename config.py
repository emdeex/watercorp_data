import os

# Financial years covered
YEARS = [
    "2014-15", "2015-16", "2016-17", "2017-18", "2018-19",
    "2019-20", "2020-21", "2021-22", "2022-23", "2023-24", "2024-25",
]

BASE_YEAR = "2024-25"

# ABS Cat. 6401.0 — All Groups CPI, Weighted Average of Eight Capital Cities
# June quarter index numbers (base: 2011-12 = 100.0)
CPI_DATA = {
    "2014-15": 107.5,
    "2015-16": 109.4,
    "2016-17": 111.4,
    "2017-18": 113.7,
    "2018-19": 115.8,
    "2019-20": 116.2,
    "2020-21": 118.8,
    "2021-22": 124.6,
    "2022-23": 133.4,
    "2023-24": 139.2,
    "2024-25": 144.5,
}

# Year at which City West Water + Western Water merged to form Greater Western Water
GWW_MERGER_YEAR = "2021-22"

# $/ML thresholds for outlier flagging
OUTLIER_LOW = 300
OUTLIER_HIGH = 10000

# Ephemeral cache directory (Vercel /tmp)
TMP_DIR = "/tmp/watercorp_reports"

# ---------------------------------------------------------------------------
# Corporation definitions
#
# Each entry has:
#   name           — display name
#   slug           — short identifier used in file paths
#   website        — base URL (no trailing slash)
#   report_pages   — list of URL paths to try when looking for the annual
#                    report listing page (tried in order)
#   is_gww_predecessor — True for the two corps that merged into GWW
# ---------------------------------------------------------------------------
CORPORATIONS = [
    {
        "name": "Barwon Water",
        "slug": "barwon",
        "website": "https://www.barwonwater.vic.gov.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/publications/annual-reports",
            "/publications/annual-reports",
        ],
        "is_gww_predecessor": False,
    },
    {
        "name": "Central Highlands Water",
        "slug": "chw",
        "website": "https://www.chw.net.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-reports",
            "/publications/annual-reports",
        ],
        "is_gww_predecessor": False,
    },
    {
        "name": "City West Water",
        "slug": "citywest",
        "website": "https://www.citywestwater.com.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-report",
            "/annual-reports",
        ],
        "is_gww_predecessor": True,
    },
    {
        "name": "Coliban Water",
        "slug": "coliban",
        "website": "https://www.coliban.com.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-reports",
            "/publications/annual-reports",
        ],
        "is_gww_predecessor": False,
    },
    {
        "name": "East Gippsland Water",
        "slug": "egw",
        "website": "https://www.egwater.vic.gov.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-reports",
            "/publications",
        ],
        "is_gww_predecessor": False,
    },
    {
        "name": "Greater Western Water",
        "slug": "gww",
        "website": "https://www.gww.com.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-reports",
            "/annual-reports",
        ],
        "is_gww_predecessor": False,
    },
    {
        "name": "Goulburn Valley Water",
        "slug": "gvw",
        "website": "https://www.gvwater.vic.gov.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-reports",
            "/publications/annual-reports",
        ],
        "is_gww_predecessor": False,
    },
    {
        "name": "Lower Murray Water",
        "slug": "lmw",
        "website": "https://www.lmw.vic.gov.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-reports",
            "/publications",
        ],
        "is_gww_predecessor": False,
    },
    {
        "name": "Melbourne Water",
        "slug": "melbwater",
        "website": "https://www.melbournewater.com.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/publications/annual-report",
            "/publications/annual-reports",
        ],
        "is_gww_predecessor": False,
    },
    {
        "name": "North East Water",
        "slug": "new",
        "website": "https://www.newater.com.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-reports",
            "/publications/annual-reports",
        ],
        "is_gww_predecessor": False,
    },
    {
        "name": "South East Water",
        "slug": "sew",
        "website": "https://www.southeastwater.com.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-reports",
            "/annual-reports",
        ],
        "is_gww_predecessor": False,
    },
    {
        "name": "Southern Rural Water",
        "slug": "srw",
        "website": "https://www.srw.com.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-reports",
            "/publications",
        ],
        "is_gww_predecessor": False,
    },
    {
        "name": "South Gippsland Water",
        "slug": "sgw",
        "website": "https://www.sgwater.com.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-reports",
            "/publications/annual-reports",
        ],
        "is_gww_predecessor": False,
    },
    {
        "name": "Wannon Water",
        "slug": "wannon",
        "website": "https://www.wannon.com.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-reports",
            "/publications/annual-reports",
        ],
        "is_gww_predecessor": False,
    },
    {
        "name": "Western Water",
        "slug": "westernwater",
        "website": "https://www.westernwater.com.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-reports",
            "/publications/annual-reports",
        ],
        "is_gww_predecessor": True,
    },
    {
        "name": "Yarra Valley Water",
        "slug": "yvw",
        "website": "https://www.yvw.com.au",
        "report_pages": [
            "/about-us/publications/annual-reports",
            "/about/annual-reports",
            "/publications/annual-reports",
        ],
        "is_gww_predecessor": False,
    },
]

# Lookup by slug
CORP_BY_SLUG = {c["slug"]: c for c in CORPORATIONS}

# Corps that should appear in the final output (non-predecessor active corps
# plus GWW — predecessors are merged in calculator.py before the merger year)
ACTIVE_CORPS = [c["name"] for c in CORPORATIONS if not c["is_gww_predecessor"]]
