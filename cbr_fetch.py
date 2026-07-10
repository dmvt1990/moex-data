#!/usr/bin/env python3
"""
cbr_fetch.py — fetch CBR macroeconomic data, output JSON to stdout.

Usage:
  python3 cbr_fetch.py --metric <metric> [--from DD.MM.YYYY] [--to DD.MM.YYYY]

Metrics: key_rate | ruonia | fx_rates | inflation | m2 | gold | trade | all

Without --from/--to: returns current/latest value only.
With --from: returns full history array from that date.
"""

import argparse
import json
import re
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

CURRENCIES = {"USD": "R01235", "EUR": "R01239", "CNY": "R01375", "GBP": "R01035"}

def fetch_fx(date_str=None, from_date=None, to_date=None):
    # Historical range: use XML_dynamic.asp per currency
    if from_date:
        result = {"from": to_iso(from_date), "to": to_iso(to_date or today())}
        for code, val_id in CURRENCIES.items():
            url = "https://cbr.ru/scripts/XML_dynamic.asp"
            params = {
                "date_req1": from_date,   # DD.MM.YYYY
                "date_req2": to_date or today(),
                "VAL_NM_RQ": val_id,
            }
            r = requests.get(url, params=params, timeout=15)
            root = ET.fromstring(r.content)
            series = []
            for rec in root.findall("Record"):
                try:
                    nominal_el = rec.find("Nominal")
                    nominal = int(nominal_el.text) if nominal_el is not None else 1
                    series.append({
                        "date": to_iso(rec.attrib["Date"]),
                        "rate": round(parse_float(rec.find("Value").text) / nominal, 4),
                    })
                except (ValueError, KeyError, AttributeError):
                    continue
            result[code] = series
        return result

    # Single date: use XML_daily.asp
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


# ── Foreign trade in goods (DataService API) ─────────────────────────────────

TRADE_PUBLICATION_ID = 9    # «Счет текущих операций»
TRADE_DATASET_ID     = 13   # «Товары» — Сальдо / Экспорт / Импорт, млн долларов США
TRADE_FIRST_YEAR     = 1994 # начало ряда по DTRange
TRADE_ROLL           = 4    # окно скользящей суммы, кварталов (= 12 месяцев)

_ROMAN_Q = {"I": 1, "II": 2, "III": 3, "IV": 4}

def _parse_quarter(dt: str):
    """«IV квартал 2012» → (2012, 4).

    Нельзя брать год из поля `date`: у IV квартала там 1 января следующего года.
    """
    m = re.match(r"\s*(IV|I{1,3})\s+квартал\s+(\d{4})", str(dt))
    if not m:
        raise ValueError(f"Cannot parse quarter: {dt!r}")
    return int(m.group(2)), _ROMAN_Q[m.group(1)]


def fetch_trade(from_date=None, to_date=None):
    """Экспорт/импорт товаров (методология платёжного баланса) через CBR DataService.

    Ряд квартальный (с 1994 г.), значения в млн долларов США. Возвращаем скользящую
    сумму за 4 квартала в млрд долларов — то есть «за 12 месяцев», как в бюджетном
    графике. Месячных данных ЦБ в машиночитаемом виде не публикует: помесячная
    разбивка есть только в оценке платёжного баланса за последний квартал.
    """
    now = datetime.now()
    url = "https://www.cbr.ru/dataservice/data"
    params = {
        "y1": TRADE_FIRST_YEAR, "y2": now.year,
        "publicationId": TRADE_PUBLICATION_ID, "datasetId": TRADE_DATASET_ID,
        "lang": "ru",
    }
    try:
        r = requests.get(url, params=params, timeout=25,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return {"error": f"Trade DataService request failed: {e}"}

    col = {}
    for h in d.get("headerData", []):
        name = str(h.get("elname", "")).strip().lower()
        if name in ("экспорт", "импорт"):
            col[name] = h["id"]
    if len(col) != 2:
        return {"error": "Trade DataService: колонки Экспорт/Импорт не найдены"}

    quarters = {}
    for x in d.get("RawData", []):
        val = x.get("obs_val")
        if val is None:
            continue
        try:
            key = _parse_quarter(x.get("dt"))
        except ValueError:
            continue
        slot = quarters.setdefault(key, {"order": str(x.get("date", ""))})
        if x.get("colId") == col["экспорт"]:
            slot["exp"] = float(val)
        elif x.get("colId") == col["импорт"]:
            slot["imp"] = float(val)

    ordered = sorted((k for k, v in quarters.items() if "exp" in v and "imp" in v),
                     key=lambda k: quarters[k]["order"])
    if len(ordered) < TRADE_ROLL:
        return {"error": "Trade DataService: недостаточно кварталов для скользящей суммы"}

    history = []
    for i in range(TRADE_ROLL - 1, len(ordered)):
        window = ordered[i - TRADE_ROLL + 1 : i + 1]
        exp = sum(quarters[k]["exp"] for k in window) / 1000.0   # млн $ → млрд $
        imp = sum(quarters[k]["imp"] for k in window) / 1000.0
        year, quarter = ordered[i]
        history.append({
            "period":      f"{year}-Q{quarter}",
            "export_bln":  round(exp, 1),
            "import_bln":  round(imp, 1),
        })

    latest = dict(history[-1])
    latest["balance_bln"] = round(latest["export_bln"] - latest["import_bln"], 1)

    return {
        "latest": latest,
        "unit": "bln_usd, trailing 4 quarters",
        "history": history,
        "source": "cbr.ru/dataservice publicationId=9 datasetId=13 «Товары»",
    }


# ── M2 (DataService API) ──────────────────────────────────────────────────────

M2_PUBLICATION_ID = 5   # «Структура денежной массы»
M2_DATASET_ID     = 7   # «Денежный агрегат М2» (header «Всего» = M2 total, млрд руб.)

def fetch_m2(from_date=None, to_date=None):
    """M2 money supply (national definition) via CBR DataService REST API.

    cbr.ru/statistics/ms/ is JS-rendered, but the DataService API behind it
    serves the series as JSON. Dataset 7 = «Денежный агрегат М2»; the «Всего»
    header is the M2 total in млрд руб. Returns the latest level + YoY %.
    """
    now = datetime.now()
    url = "https://www.cbr.ru/dataservice/data"
    params = {
        "y1": now.year - 2, "y2": now.year,
        "publicationId": M2_PUBLICATION_ID, "datasetId": M2_DATASET_ID, "lang": "ru",
    }
    try:
        r = requests.get(url, params=params, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return {"error": f"M2 DataService request failed: {e}"}

    header = d.get("headerData", [])
    total_id = next((h["id"] for h in header
                     if str(h.get("elname", "")).strip().lower() == "всего"), None)

    series = {}
    for x in d.get("RawData", []):
        if total_id is not None and x.get("colId") != total_id:
            continue
        period = str(x.get("date", ""))[:7]
        if period and x.get("obs_val") is not None:
            series[period] = float(x["obs_val"])

    if not series:
        return {"error": "M2 DataService: 'Всего' series not found"}

    latest = max(series)
    val = series[latest]
    ly, lm = latest.split("-")
    prev = series.get(f"{int(ly) - 1}-{lm}")
    yoy = round((val / prev - 1) * 100, 2) if prev else None

    return {
        "latest": {"period": latest, "value_bln_rub": round(val, 1), "yoy_pct": yoy},
        "unit": "bln_rub | %_yoy",
        "source": "cbr.ru/dataservice datasetId=7 «Всего»",
    }


# ── Gold (XML) ───────────────────────────────────────────────────────────────

GOLD_CODE = "1"  # Code=1 → Gold (Au); 2=Silver, 3=Platinum, 4=Palladium

def fetch_gold(from_date=None, to_date=None):
    """Fetch CBR official gold accounting price (учётная цена) in RUB per gram."""
    url = "https://cbr.ru/scripts/xml_metall.asp"
    params = {
        "date_req1": from_date or today(),
        "date_req2": to_date or today(),
    }
    r = requests.get(url, params=params, timeout=15)
    root = ET.fromstring(r.content)  # encoding declared in XML header

    history = []
    for rec in root.findall("Record"):
        if rec.get("Code") == GOLD_CODE:
            try:
                history.append({
                    "date": to_iso(rec.attrib["Date"]),
                    "rate": parse_float(rec.find("Buy").text),
                })
            except (ValueError, KeyError, AttributeError):
                continue

    history.sort(key=lambda x: x["date"])
    latest = history[-1] if history else None

    if from_date:
        return {"latest": latest, "unit": "RUB_per_gram", "history": history}
    return {"latest": latest, "unit": "RUB_per_gram"}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch CBR data as JSON")
    parser.add_argument("--metric", required=True,
                        choices=["key_rate", "ruonia", "fx_rates", "inflation", "m2", "gold",
                                 "trade", "all"])
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
        "fx_rates":  lambda: fetch_fx(args.to_date, args.from_date, args.to_date),
        "inflation": lambda: fetch_inflation(args.from_date, args.to_date),
        "m2":        lambda: fetch_m2(args.from_date, args.to_date),
        "gold":      lambda: fetch_gold(args.from_date, args.to_date),
        "trade":     lambda: fetch_trade(args.from_date, args.to_date),
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
