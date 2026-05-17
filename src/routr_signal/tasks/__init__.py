"""Out-of-band pipeline tasks (weekly synthesis, dispatch worker, etc.).

These tasks run on their own GitHub Actions cron, separate from the daily
fetch pipeline. They share the same SQLite intel.db (via the Actions cache)
and the same lib + classify modules.
"""
