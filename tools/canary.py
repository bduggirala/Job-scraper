"""Pre-flight check: is each major collection path still working?

Not part of the pipeline - run it before trusting a full run. A full run
takes ~15 minutes; this takes about a minute and catches the class of
breakage where one provider (or the whole browser path) silently returns
zero jobs for every company.

    python tools/canary.py
    python tools/canary.py --quiet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ats.router import fetch_company_jobs  # noqa: E402
from logger import setup_logging  # noqa: E402
from settings import load_settings  # noqa: E402

# One company per collection path, chosen because each returned jobs
# reliably in the 2026-08-21 baseline run.
CANARIES = [
    ("Capital One", "workday"),
    ("TPG", "greenhouse"),
    ("Match Group", "lever"),
    ("Texas Instruments", "taleo"),
    ("RealPage", "icims"),
    ("BNSF Railway", "phenom"),
    ("Commercial Metals Company (CMC)", "successfactors"),
    ("GameStop", "ukg"),
    ("Ryan", "playwright"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    setup_logging("logs/canary.log", "WARNING", quiet=True)
    cfg = load_settings()

    import pandas as pd

    excel_path = cfg.resolve_path("input_excel", "config/companies.xlsx")
    companies = pd.read_excel(excel_path)
    columns = cfg.get("columns", {})
    name_col = columns.get("company", "Company")
    ats_col = columns.get("ats_url", "ATS URL")
    live_col = columns.get("live_jobs_url", "Live Jobs Page (if ATS URL unavailable)")

    failures = []
    for company, expected_path in CANARIES:
        row = companies[companies[name_col].astype(str).str.strip() == company]
        if row.empty:
            print(f"  SKIP  {company:<38} not in workbook")
            continue
        record = row.iloc[0]
        try:
            result = fetch_company_jobs(
                company,
                ats_url=record.get(ats_col),
                live_jobs_url=record.get(live_col),
            )
            count = len(result.jobs)
            error = result.error_message
        except Exception as exc:  # a crash is a failure, not a stack trace
            count, error = 0, f"{type(exc).__name__}: {exc}"

        if count == 0:
            failures.append((company, expected_path, error))
        if not args.quiet or count == 0:
            status = "OK  " if count else "FAIL"
            print(f"  {status}  {company:<38} {expected_path:<15} {count:>5} jobs")

    print()
    if failures:
        print(f"CANARY FAILED: {len(failures)} of {len(CANARIES)} paths returned zero jobs")
        for company, path, error in failures:
            print(f"  {company} ({path}): {error}")
        return 1
    print(f"CANARY PASSED: all {len(CANARIES)} collection paths returned jobs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
