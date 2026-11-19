"""Best-effort history: a database outage must not terminate a background loop."""
import logging


def safe_event(db, path, state, message):
    try:
        db.event(path, state, message)
        return True
    except Exception as exc:
        # Do not log exception text: upstream errors may contain signed URLs or credentials.
        logging.getLogger(__name__).warning('History temporarily unavailable (%s)', type(exc).__name__)
        return False
