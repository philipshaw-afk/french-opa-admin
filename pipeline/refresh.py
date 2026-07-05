#!/usr/bin/env python3
"""Daily refresh for the French OPA tracker (admin site).

Fetches AMF BDIF dealing disclosures (ALL filings in the window, not just a
manual target list), downloads their PDFs, parses them, merges the results
into pipeline/state, and rebuilds index.html at the repository root.

Usage:
    python pipeline/refresh.py                    # automatic window (last state date - 5 days)
    python pipeline/refresh.py --since 2026-01-01 # explicit window (e.g. first backfill run)
    python pipeline/refresh.py --skip-fetch       # rebuild index.html only (no network)
"""
import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
TMP = ROOT / "tmp_run"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def run(cmd):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True)


def latest_state_date(raw_state):
    dates = [
        str(f.get("date_information") or f.get("online_at") or f.get("published_at") or "")[:10]
        for f in raw_state.get("filings", [])
    ]
    dates = [d for d in dates if len(d) == 10 and d.startswith("20")]
    return max(dates) if dates else "2026-01-01"


def transaction_key(row):
    return (
        row.get("amf_number"),
        row.get("transaction_date"),
        row.get("operation"),
        row.get("quantity"),
        row.get("price_eur"),
        row.get("resulting_holding") or "",
    )


def fetch_and_merge(since, limit):
    raw_state = load(STATE / "raw-filings.json")
    shutil.rmtree(TMP, ignore_errors=True)
    run([
        "node", ROOT / "fetch-amf.mjs", "--all",
        "--date-start", since,
        "--limit", str(limit),
        "--out", TMP,
    ])

    tmp_raw = load(TMP / "raw-filings.json")
    fetched = tmp_raw.get("filings", [])
    known = {f.get("amf_number") for f in raw_state.get("filings", [])}
    new_records = [f for f in fetched if f.get("amf_number") not in known]
    print(f"Fetched {len(fetched)} records since {since}; {len(new_records)} new.")

    # Parse only the new records (their PDFs sit in TMP/pdfs).
    parse_input = dict(tmp_raw)
    parse_input["filings"] = new_records
    dump(TMP / "new-raw.json", parse_input)
    run([sys.executable, ROOT / "parse-filings.py", "--input", TMP / "new-raw.json", "--out", TMP])
    parsed_new = load(TMP / "filings.json")

    state_filings = load(STATE / "filings.json")
    notice_ids = {n.get("amf_number") for n in state_filings.get("notice_summaries", [])}
    tx_keys = {transaction_key(r) for r in state_filings.get("transactions", [])}

    added_notices = 0
    for notice in parsed_new.get("notice_summaries", []):
        if notice.get("amf_number") in notice_ids:
            continue
        state_filings.setdefault("notice_summaries", []).append(notice)
        notice_ids.add(notice.get("amf_number"))
        added_notices += 1

    added_tx = 0
    for row in parsed_new.get("transactions", []):
        key = transaction_key(row)
        if key in tx_keys:
            continue
        state_filings.setdefault("transactions", []).append(row)
        tx_keys.add(key)
        added_tx += 1

    # Freshen the offer-period company list (downloaded xlsx) and source info.
    if parsed_new.get("offer_period_companies_from_amf_xlsx"):
        state_filings["offer_period_companies_from_amf_xlsx"] = parsed_new[
            "offer_period_companies_from_amf_xlsx"
        ]
    state_filings["generated_at"] = parsed_new.get("generated_at") or dt.datetime.now(
        dt.timezone.utc
    ).isoformat(timespec="seconds")
    if tmp_raw.get("source"):
        source = state_filings.setdefault("source", {})
        for key in ("bdif_endpoint", "offer_period_company_list"):
            if tmp_raw["source"].get(key):
                source[key] = tmp_raw["source"][key]

    raw_state.setdefault("filings", []).extend(new_records)
    raw_state["generated_at"] = tmp_raw.get("generated_at")
    raw_state["matched_count"] = len(raw_state["filings"])
    dump(STATE / "raw-filings.json", raw_state)
    dump(STATE / "filings.json", state_filings)
    print(f"State updated: +{added_notices} notices, +{added_tx} transactions.")


def refresh_share_capital():
    """Fetch AMF 223-16 declarations and update the share-capital history.
    Failures here must never break the daily filings refresh."""
    tmp = ROOT / "tmp_capital"
    try:
        run(["node", ROOT / "fetch_capital.mjs",
             "--state", STATE / "filings.json", "--out", tmp])
        run([sys.executable, ROOT / "parse_capital.py",
             "--input", tmp / "capital-declarations.json",
             "--state", STATE / "share_capital.json"])
    except Exception as error:  # noqa: BLE001
        print(f"WARNING: share-capital refresh failed: {error}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        default="",
        help="Fetch window start YYYY-MM-DD (blank = last state date minus 5 days)",
    )
    parser.add_argument("--limit", type=int, default=500, help="Max BDIF records to pull")
    parser.add_argument("--skip-fetch", action="store_true", help="Rebuild only, no network")
    args = parser.parse_args()

    if not args.skip_fetch:
        since = args.since.strip()
        if not since:
            last = dt.date.fromisoformat(latest_state_date(load(STATE / "raw-filings.json")))
            since = (last - dt.timedelta(days=5)).isoformat()
        fetch_and_merge(since, args.limit)
        refresh_share_capital()

    run([sys.executable, ROOT / "build_app.py"])
    shutil.rmtree(TMP, ignore_errors=True)
    print("Done.")


if __name__ == "__main__":
    main()
