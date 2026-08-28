"""Local Streamlit dashboard for the company ATS scraper.

Three modules, deliberately:

``services``
    Everything that is not UI - the cross-process run lock, launching the
    scraper, reading ``output/last_run.json`` and the current job export, and
    the safe ``config/companies.xlsx`` writer. Imports no Streamlit, so it is
    testable (and importable) on its own.
``runner``
    The supervisor process. Spawns ``main.py``, captures its console output to
    the single current dashboard log, and records the real **exit code** when
    it ends. A run is never called successful because a process started.
``app``
    The two-tab Streamlit UI. Contains no scraper logic of its own.

Nothing here reimplements the pipeline: the dashboard shells out to the same
``python main.py`` every other entry point uses, and reads the same files that
run writes.
"""
