#!/usr/bin/env python3
"""Move the tracker's data between Supabase (DIANA's project) and the daily job.

Everything the two sites use now lives in Supabase, not in the GitHub repos:

  opa-admin bucket (private, DIANA team only)
    state/<file>                 pipeline state (filings, raw filings, share capital, links ...)
    data.json.gz                 the admin site's data, loaded after sign-in
    backups/<YYYY-MM-DD>.tar.gz  a copy of state/ from each run (last 30 kept)
  opa-public bucket (public)
    live.json.gz                 the client site's data
  opa_admin_state table          edits made on the admin site (company links etc.)

Usage (run from the repository root by .github/workflows/refresh.yml):
    python pipeline/supabase_sync.py pull   # state/ -> pipeline/state, previous client data
    python pipeline/supabase_sync.py push   # pipeline/out + pipeline/state -> Supabase

Needs OPA_BOT_PASSWORD (GitHub secret): the password of the opa-bot@ma-monitor.co.uk
Supabase user, which may only write to the two opa buckets.
"""
import argparse
import datetime as dt
import gzip
import io
import json
import os
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
OUT = ROOT / "out"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://awpbhcyhfgsznmrqiddv.supabase.co").rstrip("/")
# Publishable key: safe to publish, it only identifies the project.
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_wci9xM67qfl-jdIy7e9T2Q_fKALWRdF")
BOT_EMAIL = os.environ.get("OPA_BOT_EMAIL", "opa-bot@ma-monitor.co.uk")

ADMIN_BUCKET = "opa-admin"
PUBLIC_BUCKET = "opa-public"

# Pipeline state files. raw-filings.json is saved LAST: if a save is interrupted, the
# next run re-fetches and re-parses the newest filings instead of skipping them.
STATE_FILES = [
    "company_links.json",
    "europe-filings.json",
    "five-year-filings.json",
    "french-deals-5-years.json",
    "share_capital.json",
    "zodiac-legacy-export.txt",
    "filings.json",
    "raw-filings.json",
]
BACKUP_DAYS = 30


class SupabaseError(RuntimeError):
    pass


class Client:
    def __init__(self):
        password = os.environ.get("OPA_BOT_PASSWORD", "")
        if not password:
            raise SystemExit("OPA_BOT_PASSWORD is not set (add it as a GitHub Actions secret).")
        status, body = self._call(
            "POST", "/auth/v1/token?grant_type=password",
            json.dumps({"email": BOT_EMAIL, "password": password}).encode(),
            {"Content-Type": "application/json"}, auth=False,
        )
        if status != 200:
            raise SupabaseError(f"Sign-in as {BOT_EMAIL} failed ({status}): {body[:300]!r}")
        self.token = json.loads(body)["access_token"]

    def _call(self, method, path, data=None, headers=None, auth=True):
        request = urllib.request.Request(SUPABASE_URL + path, data=data, method=method)
        request.add_header("apikey", SUPABASE_KEY)
        if auth:
            request.add_header("Authorization", f"Bearer {self.token}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                if error.code >= 500 and attempt < 2:
                    continue
                return error.code, error.read()
            except urllib.error.URLError:
                if attempt == 2:
                    raise
        raise SupabaseError(f"{method} {path} failed")

    @staticmethod
    def _object_path(bucket, name):
        return f"{bucket}/" + urllib.parse.quote(name)

    def list(self, bucket, prefix):
        status, body = self._call(
            "POST", f"/storage/v1/object/list/{bucket}",
            json.dumps({"prefix": prefix, "limit": 1000, "offset": 0,
                        "sortBy": {"column": "name", "order": "asc"}}).encode(),
            {"Content-Type": "application/json"},
        )
        if status != 200:
            raise SupabaseError(f"Listing {bucket}/{prefix} failed ({status}): {body[:300]!r}")
        return [item["name"] for item in json.loads(body) if item.get("id")]

    def download(self, bucket, name):
        status, body = self._call("GET", "/storage/v1/object/authenticated/" + self._object_path(bucket, name))
        if status == 200:
            return body
        # Storage answers a missing object with 400 + {"error": "not_found", ...}.
        if status == 404 or (status == 400 and (b"not_found" in body or b"not found" in body.lower())):
            return None
        raise SupabaseError(f"Download of {bucket}/{name} failed ({status}): {body[:300]!r}")

    def upload(self, bucket, name, data, content_type, cache_seconds=0):
        status, body = self._call(
            "POST", "/storage/v1/object/" + self._object_path(bucket, name), data,
            {"Content-Type": content_type, "x-upsert": "true",
             "cache-control": f"max-age={cache_seconds}" if cache_seconds else "no-cache"},
        )
        if status not in (200, 201):
            raise SupabaseError(f"Upload of {bucket}/{name} failed ({status}): {body[:300]!r}")
        print(f"  saved {bucket}/{name} ({len(data):,} bytes)")

    def delete(self, bucket, names):
        if not names:
            return
        status, body = self._call(
            "DELETE", f"/storage/v1/object/{bucket}",
            json.dumps({"prefixes": names}).encode(), {"Content-Type": "application/json"},
        )
        if status != 200:
            raise SupabaseError(f"Delete in {bucket} failed ({status}): {body[:300]!r}")

    def admin_setting(self, key):
        status, body = self._call(
            "GET", "/rest/v1/opa_admin_state?select=key,value,updated_at&key=eq." + urllib.parse.quote(key)
        )
        if status != 200:
            raise SupabaseError(f"Reading setting {key} failed ({status}): {body[:300]!r}")
        rows = json.loads(body)
        return rows[0] if rows else None


def gunzip_json(data):
    return json.loads(gzip.decompress(data).decode("utf-8"))


def apply_published_company_links(client):
    """The admin site's "Publish links to daily build" button saves the approved and
    rejected company-name links to Supabase (this replaced uploading company_links.json
    to the repository). Copy them into the state file before the build."""
    row = client.admin_setting("companyLinksFile")
    if not row:
        return
    path = STATE / "company_links.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if str(state.get("admin_published_at") or "") >= str(row["updated_at"]):
        return
    value = row["value"] or {}
    state["approved"] = value.get("approved", [])
    state["rejected"] = value.get("rejected", [])
    state["admin_published_at"] = row["updated_at"]
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Applied company links published from the admin site at {row['updated_at']}.")


def pull():
    client = Client()
    STATE.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    stored = set(client.list(ADMIN_BUCKET, "state"))
    for name in STATE_FILES:
        if name in stored:
            data = client.download(ADMIN_BUCKET, f"state/{name}")
            if data is None:
                raise SupabaseError(f"state/{name} is listed but could not be downloaded")
            (STATE / name).write_bytes(data)
            print(f"Loaded state/{name} ({len(data):,} bytes)")
        elif (STATE / name).exists():
            # First run after the move: start from the copy that was in the repository.
            print(f"state/{name} not in Supabase yet - starting from the repository copy")
        else:
            raise SupabaseError(f"state/{name} is missing from Supabase and the repository")

    apply_published_company_links(client)

    previous = client.download(PUBLIC_BUCKET, "live.json.gz")
    target = OUT / "previous-live.json"
    if previous is not None:
        target.write_text(json.dumps(gunzip_json(previous), ensure_ascii=False), encoding="utf-8")
        print("Loaded the previous client data.")
    elif target.exists():
        target.unlink()


def push():
    client = Client()
    data_file = OUT / "data.json"
    if not data_file.exists():
        raise SupabaseError(f"{data_file} not found - did the build run?")
    json.loads(data_file.read_text(encoding="utf-8"))  # refuse to upload a broken file
    client.upload(ADMIN_BUCKET, "data.json.gz", gzip.compress(data_file.read_bytes(), 9), "application/gzip")

    live_file = OUT / "live.json"
    if live_file.exists():
        json.loads(live_file.read_text(encoding="utf-8"))
        client.upload(PUBLIC_BUCKET, "live.json.gz", gzip.compress(live_file.read_bytes(), 9),
                      "application/gzip", cache_seconds=60)
    else:
        print("WARNING: no client data built (pipeline/out/live.json) - client site not updated")

    for name in STATE_FILES:
        path = STATE / name
        content_type = "text/plain; charset=utf-8" if name.endswith(".txt") else "application/json"
        client.upload(ADMIN_BUCKET, f"state/{name}", path.read_bytes(), content_type)

    today = dt.datetime.now(dt.timezone.utc).date()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in STATE_FILES:
            tar.add(STATE / name, arcname=f"state/{name}")
    client.upload(ADMIN_BUCKET, f"backups/{today.isoformat()}.tar.gz", buffer.getvalue(), "application/gzip")

    cutoff = (today - dt.timedelta(days=BACKUP_DAYS)).isoformat()
    old = [f"backups/{name}" for name in client.list(ADMIN_BUCKET, "backups") if name[:10] < cutoff]
    client.delete(ADMIN_BUCKET, old)
    if old:
        print(f"Removed {len(old)} backups older than {BACKUP_DAYS} days.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["pull", "push"])
    args = parser.parse_args()
    try:
        pull() if args.action == "pull" else push()
    except SupabaseError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
