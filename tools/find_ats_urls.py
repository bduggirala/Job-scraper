"""Find a working ATS URL or job-search page for companies we cannot reach.

Not part of the pipeline - an on-demand tool. Every URL it records has been
driven through a real collector and returned jobs; anything it cannot prove
is recorded as NOT FOUND for you to resolve by hand.

    python tools/find_ats_urls.py                 # every row without a verified path
    python tools/find_ats_urls.py --only-failures  # only Data Retrieved = FALSE
    python tools/find_ats_urls.py --no-browser     # HTTP stage only, much faster
    python tools/find_ats_urls.py --limit 10
    python tools/find_ats_urls.py --apply          # promote suggestions into the real columns

Never run this while a full pipeline run is in progress: both write
config/companies.xlsx.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ats.discovery import discover  # noqa: E402
from export_ats_urls import write_suggestions  # noqa: E402
from logger import setup_logging  # noqa: E402
from pipeline import load_companies, resolve_companies_path  # noqa: E402
from settings import load_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only-failures", action="store_true",
                        help="only rows whose Data Retrieved is FALSE")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true",
                        help="HTTP stage only; much faster, finds less")
    parser.add_argument("--apply", action="store_true",
                        help="promote verified suggestions into ATS URL / Live Jobs Page")
    args = parser.parse_args()

    setup_logging("logs/discovery.log", "INFO", quiet=True)
    cfg = load_settings()
    companies_path = resolve_companies_path(cfg)
    companies = load_companies(cfg, companies_path)

    # load_companies() always projects onto this fixed tidy schema
    # (pipeline.py:126-155), independent of the configured header names -
    # confirmed by inspection: companies.columns == ["company", "ats_url",
    # "live_jobs_url"] regardless of cfg["columns"]. Key on it directly.
    name_col, ats_col, live_col = "company", "ats_url", "live_jobs_url"

    # load_companies()'s tidy projection drops "Data Retrieved" entirely, so
    # --only-failures reads it straight from the workbook - the same
    # approach tools/canary.py already uses for this same column.
    status_by_company: dict[str, str] = {}
    if args.only_failures:
        import pandas as pd

        raw = pd.read_excel(companies_path)
        company_header = cfg.get("columns", {}).get("company", "Company")
        for _, raw_record in raw.iterrows():
            raw_name = str(raw_record.get(company_header) or "").strip()
            if raw_name:
                # openpyxl's TRUE/FALSE boolean cells load as native bool via
                # pandas (confirmed: dtype is bool, not str) - "x or ''"
                # would silently turn False into "", so check None explicitly.
                status_value = raw_record.get("Data Retrieved")
                status_by_company[raw_name] = (
                    "" if status_value is None else str(status_value).strip().upper()
                )

    def _blank(value) -> bool:
        return value is None or str(value).strip().lower() in {"", "nan", "none"}

    rows = []
    seen: set[str] = set()
    for _, record in companies.iterrows():
        name = str(record.get(name_col) or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)

        if args.only_failures and status_by_company.get(name) != "FALSE":
            continue

        seed = record.get(ats_col)
        if _blank(seed):
            seed = record.get(live_col)
        rows.append((name, None if _blank(seed) else str(seed).strip()))

    if args.limit:
        rows = rows[: args.limit]

    print(f"Discovering for {len(rows)} companies "
          f"({'HTTP only' if args.no_browser else 'HTTP + browser'})\n")

    results = []
    for name, seed in rows:
        found = discover(name, seed, use_browser=not args.no_browser)
        results.append(found)
        marker = "OK  " if found.jobs_found else "----"
        target = found.ats_url or found.jobs_page or "NOT FOUND"
        print(f"  {marker}  {name[:34]:<34} {found.method:<8} "
              f"{found.jobs_found:>5}  {target[:58]}")

    out_path = Path("output/ats_discovery.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["company", "ats_url", "provider", "jobs_page",
                         "jobs_found", "method", "note"])
        for found in results:
            writer.writerow([found.company, found.ats_url or "", found.provider or "",
                             found.jobs_page or "", found.jobs_found, found.method, found.note])

    verified = sum(1 for r in results if r.jobs_found)
    print(f"\nVerified {verified} of {len(results)}; wrote {out_path}")

    export = write_suggestions(
        resolve_companies_path(cfg), results, apply=args.apply
    )
    print(f"Workbook: {export['updated']} suggestion row(s), "
          f"{export['applied']} applied"
          + (f", backup {export['backup_path'].name}" if export["backup_path"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
