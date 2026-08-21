# Working in this repo

`README.md` is the **single source of truth** for how this project works —
architecture, the routing ladder, providers, CLI flags, tools, and a
file-by-file Codebase map. Read it first to orient.

## Documentation rule (important)

Keep `README.md` in sync with the code **in the same change** that alters what it
describes. Updating the doc is part of finishing the change, not a follow-up.
The README's "Keeping this doc current" section maps each kind of change to the
section it must update; in short:

- Add/change a collector or `COLLECTORS` in `ats/router.py` → providers table + Codebase map
- Change detection/resolution/routing → the "How it works" flow diagram
- Add/change a CLI flag in `main.py` → the Usage flag table
- Add/change/remove a module or `tools/` script → the Codebase map / "Entry points & tools"
- Change `config/settings.yaml`, output columns, or filtering → the matching section
- A design decision worth recording → a doc under `docs/superpowers/` (see its README index)

Do **not** create parallel top-level docs that duplicate the README; extend the
README instead. Design rationale (specs/plans) lives under `docs/superpowers/`.

## Project conventions

- **Verify, don't pattern-match.** Never ship a collector or write an ATS URL
  back to the workbook unless a real company returned real jobs through it.
- Tests are offline (network mocked). Run with `./venv/bin/pytest -q`.
- The 4 Playwright tests error unless Chromium's OS libs are installed
  (`playwright install-deps`); that's environmental, not a code failure.
