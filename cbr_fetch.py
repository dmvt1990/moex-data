#!/usr/bin/env python3
"""
cbr_fetch.py — fetch CBR macroeconomic data, output JSON to stdout.

Usage:
  python3 cbr_fetch.py --metric <metric> [--from DD.MM.YYYY] [--to DD.MM.YYYY]

Metrics: key_rate | ruonia | fx_rates | inflation | m2 | all

Without --from/--to: returns current/latest value only.
With --from: returns full history array from that date.
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print(json.dumps({"error": "Missing dependencies. Run: pip3 install requests beautifulsoup4"}))
    sys.exit(1)


def to_iso(ddmmyyyy: str) -> str:
    return datetime.strptime(ddmmyyyy, "%d.%m.%Y").strftime("%Y-%m-%d")

def to_soap_dt(ddmmyyyy: str) -> str:
    return datetime.strptime(ddmmyyyy, "%d.%m.%Y").strftime("%Y-%m-%dT00:00:00")

def parse_float(s: str) -> float:
    return float(s.replace(",", ".").replace("\xa0", "").replace(" ", "").strip())

def today() -> str:
    return datetime.now().strftime("%d.%m.%Y")

# ── Key Rate ──────────────────────────────────────────────────────────────────

def fetch_key_rate(from_date=None, to_date=None):
    url = "https://cbr.ru/hd_base/KeyRate/"
    params = {}
    if from_date:
        params = {
            "UniDbQuery.Posted": "True",
            "UniDbQuery.From": from_date,
            "UniDbQuery.To": to_date or today(),
        }

    r = requests.get(url, params=params, timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")

    table = soup.find("table")
    if not table:
        return {"error": "Table not found on key rate page"}

    rows = table.find_all("tr")[1:]
    history = []
    for row in rows:
        cols = row.find_all("td")
        if len(cols) >= 2:
            try:
                history.append({
                    "date": to_iso(cols[0].text.strip()),
                    "rate": parse_float(cols[1].text.strip()),
                })
            except ValueError:
                continue

    history.sort(key=lambda x: x["date"])
    current = history[-1] if history else None

    if from_date:
        return {"current": current["rate"], "effective_from": current["date"],
                "unit": "%_per_annum", "history": history}
    return {"current": current["rate"], "effective_from": current["date"],
            "unit": "%_per_annum"}


# ── RUONIA ────────────────────────────────────────────────────────────────────

def fetch_ruonia(from_date=None, to_date=None):
    url = "https://cbr.ru/hd_base/ruonia/"
    # Always include date params — page returns empty table without them
    params = {
        "UniDbQuery.Posted": "True",
        "UniDbQuery.From": from_date or "01.01.2025",
        "UniDbQuery.To": to_date or today(),
    }

    r = requests.get(url, params=params, timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")

    table = soup.find("table")
    if not table:
        return {"error": "Table not found on RUONIA page"}

    rows = table.find_all("tr")
    if len(rows) < 2:
        return {"error": "RUONIA table has no data rows"}

    # Table is transposed: header row contains dates, data rows contain metric values.
    # Row 0: [label header] | date1 | date2 | ...
    # Row 1: "Ставка RUONIA, % годовых" | rate1 | rate2 | ...
    header_cols = rows[0].find_all(["th", "td"])
    dates = []
    for col in header_cols[1:]:
        text = col.text.strip()
        try:
            dates.append(to_iso(text))
        except ValueError:
            dates.append(None)

    history = []
    for row in rows[1:]:
        cols = row.find_all("td")
        if not cols:
            continue
        label = cols[0].text.strip()
        if "RUONIA" in label or "Ставка" in label:
            for i, date in enumerate(dates):
                if date and i + 1 < len(cols):
                    try:
                        history.append({"date": date, "rate": parse_float(cols[i + 1].text.strip())})
                    except ValueError:
                        pass
            break

    history.sort(key=lambda x: x["date"])
    latest = history[-1] if history else None

    if from_date:
        return {"latest": latest, "history": history}
    return {"latest": latest}


# ── FX Rates ──────────────────────────────────────────────────────────────────

CURRENCIES = ["USD", "EUR", "CNY", "GBP"]

def fetch_fx(date_str=None):
    url = "https://cbr.ru/scripts/XML_daily.asp"
    params = {"date_req": date_str} if date_str else {}

    r = requests.get(url, params=params, timeout=15)
    root = ET.fromstring(r.content)

    result = {"date": to_iso(root.attrib.get("Date", ""))}
    for valute in root.findall("Valute"):
        code = valute.find("CharCode").text
        if code in CURRENCIES:
            nominal = int(valute.find("Nominal").text)
            value = parse_float(valute.find("Value").text)
            result[code] = {"rate": round(value / nominal, 4), "nominal": nominal}
    return result


# ── Inflation (HTML) ──────────────────────────────────────────────────────────

def parse_mmyyyy(s: str) -> str:
    """Convert MM.YYYY to YYYY-MM."""
    parts = s.strip().split(".")
    if len(parts) == 2:
        return f"{parts[1]}-{parts[0].zfill(2)}"
    raise ValueError(f"Cannot parse month-year: {s!r}")

def fetch_inflation(from_date=None, to_date=None):
    """Scrape YoY CPI inflation from cbr.ru/statistics/ddkp/infl/.

    Date column format: MM.YYYY. Columns: Date | Key rate | Inflation YoY% | Target%.
    """
    url = "https://cbr.ru/statistics/ddkp/infl/"
    params = {
        "UniDbQuery.Posted": "True",
        "UniDbQuery.From": from_date or "01.01.2024",
        "UniDbQuery.To": to_date or today(),
    }

    r = requests.get(url, params=params, timeout=15)
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code} from inflation page"}

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        return {"error": "Table not found on inflation page"}

    rows = table.find_all("tr")[1:]  # skip header
    history = []
    for row in rows:
        cols = row.find_all("td")
        if len(cols) >= 3:
            try:
                period = parse_mmyyyy(cols[0].text.strip())
                yoy = parse_float(cols[2].text.strip())  # col 1=key_rate, col 2=inflation YoY
                history.append({"period": period, "yoy_pct": yoy})
            except ValueError:
                continue

    history.sort(key=lambda x: x["period"])
    latest = history[-1] if history else None

    if from_date:
        return {"latest": latest, "unit": "%_yoy", "history": history}
    return {"latest": latest, "unit": "%_yoy"}


# ── M2 (HTML) ─────────────────────────────────────────────────────────────────

def fetch_m2(from_date=None, to_date=None):
    """M2 page at cbr.ru/statistics/ms/ is JavaScript-rendered — no static table available."""
    return {"error": "M2 data is JavaScript-rendered on cbr.ru and cannot be scraped statically. "
                     "Use the CBR website directly or find an alternative data source."}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch CBR data as JSON")
    parser.add_argument("--metric", required=True,
                        choices=["key_rate", "ruonia", "fx_rates", "inflation", "m2", "all"])
    parser.add_argument("--from", dest="from_date", default=None,
                        metavar="DD.MM.YYYY", help="Start date for historical queries")
    parser.add_argument("--to", dest="to_date", default=None,
                        metavar="DD.MM.YYYY", help="End date (defaults to today)")
    args = parser.parse_args()

    result = {"meta": {"fetched_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "source": "cbr.ru"}}
    errors = {}

    fetchers = {
        "key_rate":  lambda: fetch_key_rate(args.from_date, args.to_date),
        "ruonia":    lambda: fetch_ruonia(args.from_date, args.to_date),
        "fx_rates":  lambda: fetch_fx(args.to_date),
        "inflation": lambda: fetch_inflation(args.from_date, args.to_date),
        "m2":        lambda: fetch_m2(args.from_date, args.to_date),
    }

    targets = list(fetchers.keys()) if args.metric == "all" else [args.metric]

    for metric in targets:
        try:
            result[metric] = fetchers[metric]()
        except Exception as e:
            errors[metric] = str(e)

    if errors:
        result["errors"] = errors

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
