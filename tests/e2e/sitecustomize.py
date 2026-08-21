"""
sitecustomize.py — Installs the offline YouTube API stand-in in child processes.

Python imports ``sitecustomize`` automatically at interpreter start-up, which
is the only hook that fires *before* ``scripts/youtube_api.py`` runs its
module-level ``from googleapiclient.discovery import build``.  That matters:
patching after that import would leave the real function bound.

Only active when ``$YT_E2E_STATE`` is set, so this directory on PYTHONPATH is
inert for anything but the E2E run.
"""

import os

if os.environ.get("YT_E2E_STATE"):
    import fake_youtube

    fake_youtube.install()
