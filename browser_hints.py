"""Per-company memory of where the browser found a company's job list.

The browser tier rediscovers everything on every run: for a company with no
``ATS URL``, each run hops through the careers site and submits its search box
once per configured term just to *locate* the job list, then throws that answer
away. This module writes the answer down so the next run can skip straight to
it.

Two things are remembered per company:

``entry_url``
    The page the winning rows actually came from. The **destination**, never
    the route - a site that adds a navigation step ahead of its job list does
    not invalidate a stored destination, because the intermediate steps are
    never replayed.

``json_endpoint``
    A repeating JSON list call seen in network traffic, recorded even when
    :func:`ats.detector.detect_ats` does not recognise the provider. A company
    with one of these can be served over plain HTTP and leaves the browser
    tier entirely.

**A hint is a shortcut, never a commitment.** Every consumer falls through to
the full discovery path in the same run when a hint does not pan out, so a
stale hint costs one short attempt and never a company.

Deliberately a sidecar file rather than new workbook columns: the workbook is
the user's, written conservatively (``export_ats_urls`` fills blank cells only
and never clobbers a value), while hints are the pipeline's own and are
overwritten freely every run. Deleting ``data/browser_hints.json`` costs one
slow run and is the supported way to force full rediscovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from logger import get_logger
from settings import PROJECT_ROOT, load_settings

log = get_logger("hints")

#: Outcome classes for a hint attempt. Only ``CLEAN_FAILURE`` is evidence that
#: the stored destination is wrong; the others say nothing about it.
CLEAN_FAILURE = "clean_failure"     # 404/410, or loaded fine with zero rows
BLOCKED = "blocked"                 # bot challenge - says nothing about the URL
TRANSIENT = "transient"             # navigation timeout, crashed render
LOW_YIELD = "low_yield"             # rows found, but below min_yield_ratio

_lock = threading.Lock()
#: Guards the once-only load. Separate from ``_lock`` so the double-check
#: below can call ``load()``, which takes ``_lock`` itself.
_load_lock = threading.Lock()
_store: dict[str, dict[str, Any]] = {}
_loaded = False
_dirty = False

_STATS = {"used": 0, "written": 0, "invalidated": 0, "unsupported": 0}


def _path() -> Path:
    cfg = load_settings()
    raw = str(cfg.get("hints.path", "data/browser_hints.json"))
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def enabled() -> bool:
    """False disables every fast path; the pipeline behaves as it did before."""
    cfg = load_settings()
    return bool(cfg.get("hints.enabled", True))


def load(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Read the hint file into memory. A broken file is never fatal.

    A hint store that cannot be parsed is treated as empty: the run then does
    full discovery, which is exactly what it did before hints existed. Failing
    the run over a cache file would be a strictly worse outcome than ignoring
    it.
    """
    global _loaded, _store
    target = Path(path) if path else _path()
    with _lock:
        _store = {}
        _loaded = True
        if not target.exists():
            return _store
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Hint file %s is unreadable (%s); continuing without hints",
                        target, exc)
            return _store
        if not isinstance(data, dict):
            log.warning("Hint file %s is not an object; continuing without hints", target)
            return _store
        _store = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        log.debug("Loaded %s browser hints from %s", len(_store), target)
        return _store


def _ensure_loaded() -> None:
    """Load the store exactly once, safely under the browser worker threads.

    The check must happen inside a lock. Without one, two workers can both
    see an unloaded store, both call :func:`load`, and the second reset wipes
    whatever the first had already recorded - silently losing hints on
    precisely the run that discovers the most of them.
    """
    if _loaded:
        return
    with _load_lock:
        if not _loaded:
            load()


def _today() -> date:
    return datetime.now().date()


def _parse_day(value: Any) -> date | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except Exception:
        return None


def _stagger_offset(company: str, window: int) -> int:
    """Days to add to one company's expiry, so hints do not all expire at once.

    Every hint written on the same first run would otherwise expire on the
    same later run, which would spike that run's browser load back to a full
    cold discovery for every company at once. A stable hash of the company
    name spreads them across the window instead.
    """
    if window <= 1:
        return 0
    digest = hashlib.sha256(company.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % window


def is_expired(company: str, entry: dict[str, Any]) -> bool:
    """True when a hint is old enough to be re-derived from scratch."""
    cfg = load_settings()
    max_age = int(cfg.get("hints.max_age_days", 14))
    if max_age <= 0:
        return False
    stamp = _parse_day(entry.get("verified_at") or entry.get("checked_at"))
    if stamp is None:
        return True
    age_limit = max_age + _stagger_offset(company, max_age)
    return _today() > stamp + timedelta(days=age_limit)


def get(company: str) -> dict[str, Any] | None:
    """The usable hint for ``company``, or None to run full discovery.

    Returns None for a company whose job list was found to be unaddressable
    (``hint_unsupported``) and for any hint past its re-verification age, so
    both cases fall through to discovery without the caller special-casing
    them.
    """
    if not enabled():
        return None
    _ensure_loaded()
    with _lock:
        entry = _store.get(company)
        if not entry:
            return None
        snapshot = dict(entry)
    if is_expired(company, snapshot):
        log.debug("%s: hint expired; re-deriving", company)
        return None
    if snapshot.get("hint_unsupported"):
        return None
    if not (snapshot.get("entry_url") or snapshot.get("json_endpoint")):
        return None
    return snapshot


def min_rows(company: str, entry: dict[str, Any]) -> int:
    """Rows a hint attempt must return to be trusted.

    The bar is deliberately strict, because rejecting a hint costs nothing but
    today's behaviour - the company falls through to full discovery and is
    scraped exactly as it was before. A permissive ratio would buy a little
    speed at the price of silently accepting a shrunken result.
    """
    cfg = load_settings()
    ratio = float(cfg.get("hints.min_yield_ratio", 0.8))
    last = int(entry.get("jobs_last_seen") or 0)
    if last <= 0:
        return 1
    return max(1, int(last * ratio))


def record_success(company: str, *, entry_url: str | None = None,
                   json_endpoint: str | None = None, jobs: int = 0,
                   from_hint: bool = False) -> None:
    """Store (or refresh) what served this company.

    ``jobs_last_seen`` is written here and nowhere else. That is the whole
    reason a rejected hint cannot poison the baseline it is measured against:
    only a run that actually collected the company updates the number, so a
    company that genuinely shrinks settles at its new size after one
    rediscovery instead of oscillating between the two paths forever.
    """
    global _dirty
    if not enabled():
        return
    _ensure_loaded()
    with _lock:
        entry = dict(_store.get(company) or {})
        if entry_url:
            entry["entry_url"] = entry_url
        if json_endpoint:
            entry["json_endpoint"] = json_endpoint
        entry["verified_at"] = _today().isoformat()
        entry["jobs_last_seen"] = int(jobs)
        entry["consecutive_failures"] = 0
        entry.pop("hint_unsupported", None)
        entry.pop("checked_at", None)
        # A hint that has now actually served the company is proven; until
        # then it is only a candidate, and its first use is what tests it.
        if from_hint:
            entry["proven"] = True
        else:
            entry.setdefault("proven", False)
        if entry.get("entry_url") or entry.get("json_endpoint"):
            _store[company] = entry
            _dirty = True
            # Counts what discovery *learned* this run. A hint refreshing
            # itself after a successful use is not a new thing learned, and
            # counting it would make the figure meaningless on a warm run.
            if not from_hint:
                _STATS["written"] += 1


def record_failure(company: str, kind: str) -> None:
    """Apply a failed hint attempt to the store.

    Only a clean failure is evidence that the destination is wrong. A bot
    challenge says nothing about whether the URL is right, and a navigation
    timeout is the transient class the README already documents - discarding a
    good hint over either would throw away real discovery work for a reason
    unrelated to it.

    A candidate that has never once served the company and fails cleanly is
    not merely stale: its job list is not reachable by URL at all (a POST-only
    search, or an SPA that keeps search state out of the address bar). Storing
    it again next run would loop forever, so it is marked unsupported and the
    company skips the fast path until the marker ages out.
    """
    global _dirty
    if not enabled():
        return
    _ensure_loaded()
    with _lock:
        entry = _store.get(company)
        if not entry:
            return
        if kind in (BLOCKED,):
            return
        if kind == CLEAN_FAILURE and not entry.get("proven"):
            _store[company] = {
                "hint_unsupported": True,
                "checked_at": _today().isoformat(),
            }
            _dirty = True
            _STATS["unsupported"] += 1
            log.info("%s: job list is not reachable by URL; hint disabled", company)
            return
        if kind == CLEAN_FAILURE:
            _store.pop(company, None)
            _dirty = True
            _STATS["invalidated"] += 1
            log.info("%s: stored job-list URL no longer works; hint discarded", company)
            return
        # LOW_YIELD / TRANSIENT: keep the hint, but not forever.
        failures = int(entry.get("consecutive_failures") or 0) + 1
        entry["consecutive_failures"] = failures
        _dirty = True
        cfg = load_settings()
        if failures >= int(cfg.get("hints.max_failures", 2)):
            _store.pop(company, None)
            _STATS["invalidated"] += 1
            log.info("%s: hint failed %s times running; discarded", company, failures)


def note_used() -> None:
    with _lock:
        _STATS["used"] += 1


def stats() -> dict[str, int]:
    return dict(_STATS)


def reset_for_tests() -> None:
    """Clear module state. Tests only."""
    global _loaded, _store, _dirty
    with _lock:
        _store = {}
        _loaded = False
        _dirty = False
        for key in _STATS:
            _STATS[key] = 0


def flush(path: str | Path | None = None) -> bool:
    """Write the store to disk atomically. Returns True when it wrote.

    Called once at the end of a run rather than per company: the three browser
    worker threads all record into the in-memory store under a lock, and a
    single write at the end keeps them off the filesystem entirely.
    """
    global _dirty
    if not enabled() or not _dirty:
        return False
    target = Path(path) if path else _path()
    with _lock:
        payload = json.dumps(_store, indent=2, sort_keys=True)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, target)
        except Exception as exc:
            log.warning("Could not write hint file %s (%s)", target, exc)
            return False
        _dirty = False
    log.info("Wrote %s browser hints to %s", len(_store), target)
    return True
