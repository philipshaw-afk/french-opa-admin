#!/usr/bin/env python3
"""One-off repair: re-read the declarant names of the stored AMF notices by position.

The line-by-line PDF reading cut many declarant names short ("Management LP",
"Pictet Asset", "et consignations") or lost them (rows with a blank filer). This
downloads each stored notice's PDF from the AMF again, reads the names with
declarant_names.py and puts the full names on the stored rows. Only the filer names
change; quantities, prices and dates are left exactly as they are. A changed row keeps
its old name in "filer_as_parsed".

    python pipeline/repair_declarant_names.py --mode report   # list the changes only
    python pipeline/repair_declarant_names.py --mode apply    # also save them in pipeline/state

Run by .github/workflows/repair-declarant-names.yml after supabase_sync.py pull. In
apply mode it first saves a copy of the state in Supabase
(backups/<date>-before-name-repair.tar.gz). The report goes to
pipeline/out/declarant-name-repair.json and to the job summary.
"""
import argparse
import datetime as dt
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import declarant_names

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
OUT = ROOT / "out"
REPORT = OUT / "declarant-name-repair.json"
FILES = ["filings.json", "five-year-filings.json"]
USER_AGENT = "AMF OPA proof-of-concept scraper"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def download(url, attempts=4):
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"user-agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
        time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


def read_notice(amf_number, url):
    """Worker: download one notice and read its dealing rows with positional names."""
    try:
        data = download(url)
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(data)
            handle.flush()
            return amf_number, declarant_names.read_pdf(handle.name), None
    except Exception as error:  # noqa: BLE001
        return amf_number, None, f"{type(error).__name__}: {error}"[:300]


def backup_state(label):
    """Save the state as loaded (before any change) in Supabase."""
    import supabase_sync

    client = supabase_sync.Client()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in supabase_sync.STATE_FILES:
            if (STATE / name).exists():
                tar.add(STATE / name, arcname=f"state/{name}")
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    client.upload(supabase_sync.ADMIN_BUCKET, f"backups/{today}-{label}.tar.gz",
                  buffer.getvalue(), "application/gzip")


def markdown_summary(report):
    lines = [
        f"## Declarant name repair ({report['mode']})",
        "",
        f"Notices checked: {report['notices_checked']:,} of {report['notices']:,} "
        f"(failed to download/read: {len(report['failed']):,}; no names found: {len(report['no_names']):,})",
        f"Rows renamed: {report['rows_renamed']:,} in {report['notices_renamed']:,} notices. "
        f"Conflicts left unchanged: {report['rows_in_conflict']:,} rows.",
        "",
        "| Old name | New name | Rows | Notices | Targets |",
        "|---|---|---:|---:|---|",
    ]
    for item in report["renames"]:
        lines.append(f"| {item['old'] or '(blank)'} | {item['new']} | {item['rows']} | "
                     f"{item['notice_count']} | {', '.join(item['targets'][:3])} |")
    if report["conflicts"]:
        kept = "replaced" if report.get("conflicts_replaced") else "old name kept"
        lines += ["", f"### Conflicts ({kept})", "",
                  "| Old name | Name read by position | Rows | Example notices |", "|---|---|---:|---|"]
        for item in report["conflicts"]:
            lines.append(f"| {item['old']} | {item['new']} | {item['rows']} | {', '.join(item['notices'][:3])} |")
    if report["failed"]:
        lines += ["", "### Failed notices", ""]
        lines += [f"- {item['amf_number']}: {item['error']}" for item in report["failed"][:50]]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["report", "apply"], default="report")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="Check only the first N notices (testing)")
    parser.add_argument("--amf", default="", help="Comma-separated AMF numbers to check (testing)")
    parser.add_argument("--no-backup", action="store_true", help="Apply without the Supabase backup")
    parser.add_argument("--replace-conflicts", action="store_true",
                        help="Also replace names that conflict (only after checking the report's conflicts)")
    args = parser.parse_args()

    files = {name: load(STATE / name) for name in FILES if (STATE / name).exists()}
    urls = {}
    targets = {}
    for data in files.values():
        for row in data.get("transactions", []):
            amf, url = row.get("amf_number"), row.get("document_url") or ""
            if amf and "bdif.amf-france.org" in url and amf not in urls:
                urls[amf] = url
                targets[amf] = row.get("target") or ""
    wanted = sorted(urls, reverse=True)
    if args.amf:
        only = {a.strip() for a in args.amf.split(",") if a.strip()}
        wanted = [a for a in wanted if a in only]
    if args.limit:
        wanted = wanted[: args.limit]
    print(f"{len(urls):,} notices with an AMF PDF; checking {len(wanted):,}.", flush=True)

    if args.mode == "apply" and not args.no_backup:
        backup_state("before-name-repair")

    operations, failed = {}, []
    started = time.time()
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(read_notice, amf, urls[amf]) for amf in wanted]
        for done, future in enumerate(as_completed(futures), start=1):
            amf, ops, error = future.result()
            if error:
                failed.append({"amf_number": amf, "error": error})
            else:
                operations[amf] = ops
            if done % 250 == 0 or done == len(futures):
                print(f"  read {done:,}/{len(futures):,} notices ({time.time() - started:.0f}s)", flush=True)

    renames = defaultdict(lambda: {"rows": 0, "notices": set(), "targets": set()})
    conflicts = defaultdict(lambda: {"rows": 0, "notices": set()})
    conflict_rows = []
    no_names = []
    renamed_notices = set()
    for name, data in files.items():
        rows_by_notice = defaultdict(list)
        for row in data.get("transactions", []):
            if row.get("amf_number") in operations:
                rows_by_notice[row["amf_number"]].append(row)
        for amf, rows in rows_by_notice.items():
            ops = operations[amf]
            if not any(op.get("filer") for op in ops):
                no_names.append(amf)
                continue
            stats = declarant_names.relabel_rows(rows, ops, replace_conflicts=args.replace_conflicts)
            for old, new in stats["changed"]:
                entry = renames[(old, new)]
                entry["rows"] += 1
                entry["notices"].add(amf)
                entry["targets"].add(targets.get(amf, ""))
                renamed_notices.add(amf)
            conflict_rows += [{"amf_number": amf, "file": name, **row} for row in stats.get("conflict_rows", [])]
            for old, new in stats["conflicts"]:
                entry = conflicts[(old, new)]
                entry["rows"] += 1
                entry["notices"].add(amf)

    # How many rows still carry each old name after the renames (0 = the name is gone).
    still = Counter(
        row.get("filer") or ""
        for data in files.values()
        for row in data.get("transactions", [])
    )

    report = {
        "mode": args.mode,
        "conflicts_replaced": bool(args.replace_conflicts),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "notices": len(urls),
        "notices_checked": len(operations),
        "failed": sorted(failed, key=lambda f: f["amf_number"]),
        "no_names": sorted(set(no_names)),
        "rows_renamed": sum(v["rows"] for v in renames.values()),
        "notices_renamed": len(renamed_notices),
        "rows_in_conflict": sum(v["rows"] for v in conflicts.values()),
        "renames": sorted(
            ({"old": old, "new": new, "rows": v["rows"], "notice_count": len(v["notices"]),
              "old_rows_left": still.get(old, 0),
              "notices": sorted(v["notices"])[:20], "targets": sorted(t for t in v["targets"] if t)}
             for (old, new), v in renames.items()),
            key=lambda item: (-item["rows"], item["new"], item["old"]),
        ),
        "conflicts": sorted(
            ({"old": old, "new": new, "rows": v["rows"], "notices": sorted(v["notices"])[:20]}
             for (old, new), v in conflicts.items()),
            key=lambda item: (-item["rows"], item["old"]),
        ),
        "conflict_rows": conflict_rows,
    }
    if args.amf:
        # Checking particular notices: include what was read from each PDF.
        report["operations"] = {amf: operations.get(amf) for amf in wanted}
    OUT.mkdir(parents=True, exist_ok=True)
    dump(REPORT, report)
    summary = markdown_summary(report)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(summary)

    if args.mode == "apply":
        for name, data in files.items():
            dump(STATE / name, data)
        print(f"Saved the renamed rows in {', '.join(files)}.")
    else:
        print("Report only: nothing saved.")


if __name__ == "__main__":
    sys.exit(main())
