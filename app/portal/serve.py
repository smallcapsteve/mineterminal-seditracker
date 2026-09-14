"""Process entry point for the SediTracker portal (2026-09-14).

The unit runs `uvicorn portal.serve:app`, not `uvicorn portal.app:app`. Same
application object, plus the blocks that live in modules of their own — today
just the /api/search type-ahead behind the nav search box (ST_TYPEAHEAD_V1).

Point the unit back at portal.app:app and the site still runs, unchanged, minus
the search dropdown. Nothing else depends on this file. The matching file on
MNT is /opt/mnt/app/portal/serve.py and carries the longer note.
"""
from portal.app import app
from portal import typeahead

typeahead.register(app)

__all__ = ["app"]
