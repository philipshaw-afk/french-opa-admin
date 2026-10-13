# Pipeline state lives in Supabase

The pipeline's state files are no longer kept in this repository. They live in
DIANA's Supabase project, in the private storage bucket `opa-admin`, folder `state/`:

company_links.json, europe-filings.json, filings.json, five-year-filings.json,
french-deals-5-years.json, raw-filings.json, share_capital.json, zodiac-legacy-export.txt

The daily workflow downloads them here (`python pipeline/supabase_sync.py pull`),
runs the refresh, and saves them back (`push`), with a dated copy in `backups/`
(last 30 days).

To replace one by hand (e.g. a new french-deals-5-years.json), upload it in the
Supabase dashboard: Storage > opa-admin > state > Upload, replacing the old file.
The next daily refresh uses it.
