"""Retry wrapper for Google API .execute() calls that handles 429 rate limits.

Usage:
    from sheets_retry import retry_execute

    # Instead of:  request.execute()
    # Write:       retry_execute(request)

    result = retry_execute(
        svc.spreadsheets().values().get(spreadsheetId=sid, range=tab)
    )
"""

import time
import logging

from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

# Defaults: up to 4 retries, starting at 5s backoff, doubling each time.
# Total max wait: 5 + 10 + 20 + 40 = 75s — well within the 10-min timeout.
MAX_RETRIES = 4
INITIAL_BACKOFF = 5  # seconds


def retry_execute(request, *, max_retries: int = MAX_RETRIES,
                  initial_backoff: float = INITIAL_BACKOFF):
    """Execute a Google API request, retrying on 429 (rate limit) errors.

    Args:
        request:         A Google API HttpRequest (the object returned before
                         calling .execute()).
        max_retries:     How many times to retry on 429.
        initial_backoff: Seconds to wait on the first retry (doubles each time).

    Returns:
        The API response (same as request.execute()).

    Raises:
        HttpError: Re-raised if the error is not a 429 or retries are exhausted.
    """
    backoff = initial_backoff
    for attempt in range(max_retries + 1):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status != 429 or attempt == max_retries:
                raise
            log.warning(
                "Rate limited (429) on attempt %d/%d — retrying in %.0fs",
                attempt + 1, max_retries + 1, backoff,
            )
            time.sleep(backoff)
            backoff *= 2
