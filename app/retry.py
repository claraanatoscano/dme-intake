"""One retry policy for every outbound call: exponential backoff with jitter, retry only transient errors."""
import logging

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from . import config

log = logging.getLogger("intake.retry")


class PermanentError(Exception):
    """An error that retrying will not fix (bad input, 4xx auth/validation)."""


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, PermanentError):
        return False
    if isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    # Anthropic SDK errors expose .status_code; connection errors have none
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status == 429 or status >= 500
    name = type(exc).__name__
    return name in {"APIConnectionError", "APITimeoutError", "InternalServerError", "OverloadedError", "RateLimitError"}


def call_with_retry(fn, *args, step: str, counter: list | None = None, wait_initial: float = 1.0, **kwargs):
    """Run fn with retries. `counter` (a 1-item list) accumulates attempts for the run log."""

    def _before(state: RetryCallState):
        if counter is not None:
            counter[0] += 1
        if state.attempt_number > 1:
            log.warning("retrying %s (attempt %s)", step, state.attempt_number)

    for attempt in Retrying(
        stop=stop_after_attempt(config.RETRY_ATTEMPTS),
        wait=wait_exponential_jitter(initial=wait_initial, max=20),
        retry=retry_if_exception(is_transient),
        before=_before,
        reraise=True,
    ):
        with attempt:
            return fn(*args, **kwargs)
