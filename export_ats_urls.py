"""Write newly discovered ATS URLs back into the input workbook.

When the Playwright search fallback (``browser/playwright_scraper.py``)
uncovers a real ATS behind a branded career page, that discovery only helps
*this* run's diagnostics unless it's persisted somewhere the router will look
next time. This module fills the blank ``ATS URL`` cells in
``config/companies.xlsx`` in place - never overwriting a value already there,
so manual edits to the workbook are always safe - after backing the file up.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from logger import get_logger

log = get_logger("export_ats_urls")


def _blank(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip().lower()
    return text in {"", "nan", "none", "n/a", "na", "-", "null"}


def _backup_path(companies_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return companies_path.with_name(f"{companies_path.name}.bak-{timestamp}")


def write_discovered_urls(
    companies_path: Path | str,
    discoveries: dict[str, str],
    *,
    company_column: str = "Company",
    ats_url_column: str = "ATS URL",
) -> dict[str, Any]:
    """Fill blank ATS URL cells for companies discovered this run.

    Args:
        companies_path: path to the workbook to update in place.
        discoveries: ``{company_name: discovered_ats_url}``.
        company_column / ats_url_column: header names to match, matching the
          columns used throughout the pipeline's ``config/settings.yaml``.

    Returns:
        ``{"updated": int, "backup_path": Path | None}``. ``updated`` counts
        cells actually written - a company with no blank ATS URL cell (e.g.
        already resolved by a prior run) is not double-counted.

    Never raises for a missing/malformed workbook path issue that isn't
    fixable here; callers treat this as a best-effort convenience step, not a
    required part of the run.
    """
    path = Path(companies_path)
    if not discoveries or not path.exists():
        return {"updated": 0, "backup_path": None}

    try:
        workbook = load_workbook(path)
        sheet = workbook.active

        header_row = next(sheet.iter_rows(min_row=1, max_row=1))
        headers = {str(cell.value).strip(): cell.column for cell in header_row if cell.value}

        company_col = headers.get(company_column)
        ats_col = headers.get(ats_url_column)
        if not company_col or not ats_col:
            log.warning(
                "Workbook %s missing expected columns (%s, %s); skipping write-back",
                path.name, company_column, ats_url_column,
            )
            return {"updated": 0, "backup_path": None}

        updated = 0
        for row in sheet.iter_rows(min_row=2):
            company_cell = row[company_col - 1]
            ats_cell = row[ats_col - 1]

            company_name = str(company_cell.value).strip() if company_cell.value else ""
            if not company_name or company_name not in discoveries:
                continue
            if not _blank(ats_cell.value):
                continue

            ats_cell.value = discoveries[company_name]
            updated += 1

        if updated == 0:
            return {"updated": 0, "backup_path": None}

        backup_path = _backup_path(path)
        shutil.copy2(path, backup_path)
        workbook.save(path)
        log.info("Wrote %s discovered ATS URL(s) to %s (backup: %s)",
                  updated, path.name, backup_path.name)
        return {"updated": updated, "backup_path": backup_path}

    except Exception as exc:
        log.warning("Could not write discovered ATS URLs back to %s: %s", path, exc)
        return {"updated": 0, "backup_path": None}


def write_run_status(
    companies_path: Path | str,
    statuses: dict[str, bool],
    *,
    company_column: str = "Company",
    status_column: str = "Data Retrieved",
) -> dict[str, Any]:
    """Record whether each company yielded jobs on the latest full run.

    Unlike :func:`write_discovered_urls`, this **overwrites** the cell every
    run: it reports the outcome of the most recent run, not a value the user
    curated, so a stale TRUE from three runs ago would be worse than useless.
    The column is created if the workbook does not already have it.

    Args:
        companies_path: path to the workbook to update in place.
        statuses: ``{company_name: True_if_jobs_were_collected}``.
        company_column / status_column: header names to match.

    Returns:
        ``{"updated": int, "backup_path": Path | None}``.

    Best-effort, like the ATS write-back: a failure here never fails the run.
    """
    path = Path(companies_path)
    if not statuses or not path.exists():
        return {"updated": 0, "backup_path": None}

    try:
        workbook = load_workbook(path)
        sheet = workbook.active

        header_row = next(sheet.iter_rows(min_row=1, max_row=1))
        headers = {str(cell.value).strip(): cell.column for cell in header_row if cell.value}

        company_col = headers.get(company_column)
        if not company_col:
            log.warning("Workbook %s missing %s column; skipping status write-back",
                        path.name, company_column)
            return {"updated": 0, "backup_path": None}

        status_col = headers.get(status_column)
        if not status_col:
            status_col = sheet.max_column + 1
            sheet.cell(row=1, column=status_col, value=status_column)

        updated = 0
        for row in sheet.iter_rows(min_row=2):
            company_cell = row[company_col - 1]
            company_name = str(company_cell.value).strip() if company_cell.value else ""
            if not company_name or company_name not in statuses:
                continue
            sheet.cell(
                row=company_cell.row,
                column=status_col,
                value="TRUE" if statuses[company_name] else "FALSE",
            )
            updated += 1

        if updated == 0:
            return {"updated": 0, "backup_path": None}

        backup_path = _backup_path(path)
        shutil.copy2(path, backup_path)
        workbook.save(path)
        log.info("Wrote retrieval status for %s company row(s) to %s (backup: %s)",
                 updated, path.name, backup_path.name)
        return {"updated": updated, "backup_path": backup_path}

    except Exception as exc:
        log.warning("Could not write run status back to %s: %s", path, exc)
        return {"updated": 0, "backup_path": None}
