#!/usr/bin/env python3
"""
refresh_dashboard.py
Queries BigQuery (product-dashboard-487908.used_car_metrics) and updates
used_car_dashboard.html with fresh data. Run daily via Windows Task Scheduler.

FIRST-TIME SETUP
----------------
1. Install Python 3.9+  (winget install Python.Python.3.12)
2. Install dependencies: pip install google-cloud-bigquery
3. Set credentials — choose ONE option:
   a) Service account key (recommended for scheduled tasks):
      - Download a JSON key from Google Cloud Console > IAM > Service Accounts
      - Set GOOGLE_APPLICATION_CREDENTIALS=C:\path\to\key.json  (in Windows env vars)
   b) User credentials (interactive, one-time):
      - Install gcloud CLI, then run: gcloud auth application-default login
"""

import re
import sys
import logging
from datetime import datetime
from pathlib import Path

# ── Logging (also writes to refresh.log next to this script) ────────────────
LOG_FILE = Path(__file__).parent / "refresh.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

try:
    from google.cloud import bigquery
except ImportError:
    log.error("google-cloud-bigquery not installed.")
    log.error("Run:  pip install google-cloud-bigquery")
    sys.exit(1)

# ── Config ───────────────────────────────────────────────────────────────────
PROJECT_ID     = "product-dashboard-487908"
DATASET        = "used_car_metrics"
DASHBOARD_HTML = Path(__file__).parent / "used_car_dashboard.html"

# Look-back window: last N months of data (inclusive of partial current month)
LOOKBACK_MONTHS = 8


# ── BigQuery client ──────────────────────────────────────────────────────────
def get_client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT_ID)


# ── Month key helpers ────────────────────────────────────────────────────────
def month_key(ym: str) -> str:
    """'2025-11' → \"Nov'25\""""
    d = datetime.strptime(ym + "-01", "%Y-%m-%d")
    return d.strftime("%b'%y")


def month_to_display(mk: str) -> str:
    """\"Nov'25\" → 'Nov 2025'"""
    d = datetime.strptime(mk.replace("'", "20"), "%b%Y")
    return d.strftime("%b %Y")


# ── SQL ───────────────────────────────────────────────────────────────────────
def _sql_monthly_source() -> str:
    return f"""
SELECT
  FORMAT_DATE('%Y-%m', DATE(Date))              AS month_sort,
  SUM(CASE WHEN Source = 'M-Site'  THEN 1 ELSE 0 END) AS msite,
  SUM(CASE WHEN Source = 'Android' THEN 1 ELSE 0 END) AS android,
  SUM(CASE WHEN Source = 'IOS'     THEN 1 ELSE 0 END) AS ios,
  SUM(CASE WHEN Source = 'Botify'  THEN 1 ELSE 0 END) AS botify,
  SUM(CASE WHEN Source = 'Web'     THEN 1 ELSE 0 END) AS web,
  COUNT(*)                                             AS total
FROM `{PROJECT_ID}.{DATASET}.trustmark_leads_raw`
WHERE Source NOT IN ('Zighwheels', 'ZigWheels')
  AND DATE(Date) >= DATE_SUB(CURRENT_DATE(), INTERVAL {LOOKBACK_MONTHS} MONTH)
GROUP BY month_sort
ORDER BY month_sort
"""


def _sql_state_leads() -> str:
    return f"""
SELECT
  FORMAT_DATE('%Y-%m', DATE(Date)) AS month_sort,
  state_list_name                  AS state,
  COUNT(*)                         AS leads
FROM `{PROJECT_ID}.{DATASET}.trustmark_leads_raw`
WHERE Source NOT IN ('Zighwheels', 'ZigWheels')
  AND DATE(Date) >= DATE_SUB(CURRENT_DATE(), INTERVAL {LOOKBACK_MONTHS} MONTH)
  AND state_list_name IS NOT NULL
  AND state_list_name != ''
GROUP BY month_sort, state
ORDER BY month_sort, leads DESC
"""


def _sql_listing_t2l() -> str:
    return f"""
SELECT
  City          AS city,
  State         AS state,
  Listing_Gruop AS seller,
  COUNT(*)      AS listings,
  SUM(CASE WHEN IFNULL(Total_Leads, 0) = 0 THEN 1 ELSE 0 END) AS zero_lead,
  SUM(IFNULL(Total_Leads, 0))                                  AS leads
FROM `{PROJECT_ID}.{DATASET}.Dealer_Central_listing_raw`
WHERE Listing_Flag = 'Active_listing'
  AND City IS NOT NULL AND City != ''
  AND State IS NOT NULL AND State != ''
  AND Listing_Gruop IN ('Individual', 'Partner', 'Corporate')
GROUP BY city, state, seller
HAVING listings >= 100
ORDER BY leads DESC
LIMIT 50
"""


def _sql_renewals() -> str:
    return f"""
SELECT
  FORMAT_DATE('%Y-%m', DATE(End_Date)) AS month_sort,
  COUNT(id)                            AS due,
  SUM(is_renewed)                      AS renewed,
  COUNT(id) - SUM(is_renewed)          AS churned,
  ROUND(SUM(is_renewed) * 100.0 / COUNT(id), 1) AS rate
FROM `{PROJECT_ID}.{DATASET}.Dealer_Central_Renewal_Raw`
WHERE DATE(End_Date) >= DATE_SUB(CURRENT_DATE(), INTERVAL {LOOKBACK_MONTHS} MONTH)
  AND DATE(End_Date) < CURRENT_DATE()
  AND Pack_Type = 'Classified'
GROUP BY month_sort
ORDER BY month_sort
"""


# ── Fetchers ─────────────────────────────────────────────────────────────────
def fetch_monthly_source(client: bigquery.Client) -> list[dict]:
    rows = list(client.query(_sql_monthly_source()).result())
    return [
        {
            "month":   month_key(r.month_sort),
            "M-Site":  int(r.msite),
            "Android": int(r.android),
            "IOS":     int(r.ios),
            "Botify":  int(r.botify),
            "Web":     int(r.web),
            "total":   int(r.total),
        }
        for r in rows
    ]


def fetch_state_monthly(client: bigquery.Client) -> tuple[list[dict], list[str]]:
    rows = list(client.query(_sql_state_leads()).result())

    months_order: list[tuple[str, str]] = []
    month_seen: set[str] = set()
    cell: dict[str, dict[str, int]] = {}

    for r in rows:
        mk = month_key(r.month_sort)
        if mk not in month_seen:
            month_seen.add(mk)
            months_order.append((r.month_sort, mk))
        if r.state not in cell:
            cell[r.state] = {}
        cell[r.state][mk] = int(r.leads)

    months_order.sort(key=lambda x: x[0])
    months = [mk for _, mk in months_order]

    state_totals = {s: sum(v.values()) for s, v in cell.items()}
    states_sorted = sorted(cell.keys(), key=lambda s: -state_totals[s])

    result = [
        {"state": s, **{mk: cell[s].get(mk, 0) for _, mk in months_order}}
        for s in states_sorted
    ]
    return result, months


def fetch_listing_t2l(client: bigquery.Client) -> list[dict]:
    rows = list(client.query(_sql_listing_t2l()).result())
    return [
        {
            "city":     r.city,
            "state":    r.state,
            "seller":   r.seller,
            "listings": int(r.listings),
            "zeroLead": int(r.zero_lead),
            "leads":    int(r.leads),
            "t2l":      round(r.leads / r.listings, 1) if r.listings else 0.0,
        }
        for r in rows
    ]


def fetch_renewals(client: bigquery.Client) -> list[dict]:
    rows = list(client.query(_sql_renewals()).result())
    return [
        {
            "month":   month_key(r.month_sort),
            "due":     int(r.due),
            "renewed": int(r.renewed),
            "churned": int(r.churned),
            "rate":    float(r.rate),
        }
        for r in rows
    ]


# ── JS serialisers ────────────────────────────────────────────────────────────
def _js_str_array(lst: list[str]) -> str:
    return "[" + ",".join(f'"{x}"' for x in lst) + "]"


def _js_monthly_source(data: list[dict]) -> str:
    lines = [
        f'  {{month:"{r["month"]}",'
        f'"M-Site":{r["M-Site"]},'
        f'Android:{r["Android"]},'
        f'IOS:{r["IOS"]},'
        f'Botify:{r["Botify"]},'
        f'Web:{r["Web"]},'
        f'total:{r["total"]}}}'
        for r in data
    ]
    return "[\n" + ",\n".join(lines) + "\n]"


def _js_state_monthly(data: list[dict], months: list[str]) -> str:
    lines = []
    for r in data:
        month_pairs = ",".join(f'"{m}":{r.get(m, 0)}' for m in months)
        lines.append(f'  {{state:"{r["state"]}",{month_pairs}}}')
    return "[\n" + ",\n".join(lines) + "\n]"


def _js_listing_t2l(data: list[dict]) -> str:
    lines = [
        f'  {{city:"{r["city"]}",state:"{r["state"]}",seller:"{r["seller"]}",'
        f'listings:{r["listings"]},zeroLead:{r["zeroLead"]},'
        f'leads:{r["leads"]},t2l:{r["t2l"]}}}'
        for r in data
    ]
    return "[\n" + ",\n".join(lines) + "\n]"


def _js_renewal_monthly(data: list[dict]) -> str:
    lines = [
        f'  {{month:"{r["month"]}",due:{r["due"]},'
        f'renewed:{r["renewed"]},churned:{r["churned"]},rate:{r["rate"]}}}'
        for r in data
    ]
    return "[\n" + ",\n".join(lines) + "\n]"


# ── HTML patcher ──────────────────────────────────────────────────────────────
def _patch_const(html: str, name: str, new_value: str) -> str:
    """Replace  const NAME = [...];  with new_value."""
    pattern     = rf'(const {re.escape(name)}\s*=\s*)(\[.*?\])(;)'
    replacement = rf'\g<1>{new_value}\3'
    result, n   = re.subn(pattern, replacement, html, flags=re.DOTALL)
    if n == 0:
        raise ValueError(f"Pattern for const {name} not found in HTML")
    return result


def patch_html(
    html: str,
    monthly_source: list[dict],
    state_monthly:  list[dict],
    months:         list[str],
    listing_t2l:    list[dict],
    renewals:       list[dict],
) -> str:
    # Data arrays
    html = _patch_const(html, "MONTHS",        _js_str_array(months))
    html = _patch_const(html, "STATES",        _js_str_array([r["state"] for r in state_monthly]))
    html = _patch_const(html, "MONTHLY_SOURCE",_js_monthly_source(monthly_source))
    html = _patch_const(html, "STATE_MONTHLY", _js_state_monthly(state_monthly, months))
    html = _patch_const(html, "LISTING_T2L",   _js_listing_t2l(listing_t2l))
    html = _patch_const(html, "RENEWAL_MONTHLY",_js_renewal_monthly(renewals))

    # Header: "Last refreshed" date
    today_str = datetime.now().strftime("%b %d, %Y")
    html = re.sub(
        r'(<strong>)[^<]+(</strong>)(\s*<span[^>]*>ETL)',
        rf'\g<1>{today_str}\2\3',
        html,
        count=1,
    )

    # Header subtitle: data range + total leads
    if months:
        first_disp = month_to_display(months[0])
        last_disp  = month_to_display(months[-1])
        total_leads_fmt = f"{sum(r['total'] for r in monthly_source) / 1e6:.1f}M+"
        html = re.sub(
            r'Data:\s*[\w\s]+–[\w\s]+&nbsp;·&nbsp;',
            f'Data: {first_disp} – {last_disp} &nbsp;·&nbsp;',
            html,
            count=1,
        )
        html = re.sub(
            r'\d+\.\d+M\+ leads',
            f'{total_leads_fmt} leads',
            html,
            count=1,
        )

    return html


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=== Dashboard refresh started ===")

    if not DASHBOARD_HTML.exists():
        log.error("HTML not found: %s", DASHBOARD_HTML)
        sys.exit(1)

    try:
        client = get_client()
        log.info("BigQuery client ready (project: %s)", PROJECT_ID)
    except Exception as e:
        log.error("Could not create BigQuery client: %s", e)
        log.error("Set GOOGLE_APPLICATION_CREDENTIALS to a service account JSON key,")
        log.error("or run: gcloud auth application-default login")
        sys.exit(1)

    try:
        log.info("Querying monthly source leads …")
        monthly_source = fetch_monthly_source(client)
        log.info("  → %d months", len(monthly_source))

        log.info("Querying state-monthly leads …")
        state_monthly, months = fetch_state_monthly(client)
        log.info("  → %d states, months: %s", len(state_monthly), months)

        log.info("Querying listing T2L …")
        listing_t2l = fetch_listing_t2l(client)
        log.info("  → %d city×seller rows", len(listing_t2l))

        log.info("Querying renewals …")
        renewals = fetch_renewals(client)
        log.info("  → %d renewal months", len(renewals))
    except Exception as e:
        log.error("BigQuery query failed: %s", e)
        sys.exit(1)

    try:
        html = DASHBOARD_HTML.read_text(encoding="utf-8")
        html = patch_html(html, monthly_source, state_monthly, months, listing_t2l, renewals)
        DASHBOARD_HTML.write_text(html, encoding="utf-8")
        log.info("Dashboard updated: %s", DASHBOARD_HTML)
    except Exception as e:
        log.error("Failed to patch HTML: %s", e)
        sys.exit(1)

    log.info("=== Done ===\n")


if __name__ == "__main__":
    main()
