"""Retry only a buffered model call, before dispatching any tools from that call."""
import asyncio
from core.backends.provider_failure import describe_failure

MAX_ATTEMPTS = 3
MAX_DELAY = 30.0
sleep = asyncio.sleep  # injectable without changing asyncio globally


def retry_delay(failure, attempt):
    if not failure.retryable or attempt >= MAX_ATTEMPTS - 1:
        return None
    delay = max(float(2 ** attempt), failure.retry_after or 0.0)
    # Never shorten a provider's delay to fit our cap: stop instead.
    return delay if delay <= MAX_DELAY else None


async def model_call(call, *, writer=None, remaining=None, can_retry=lambda: True, flush=None):
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await call()
        except Exception as exc:
            failure = describe_failure(exc)
            if failure is None:
                raise
            if getattr(exc, 'failure', None) is None:
                failure.log()
            if getattr(exc, 'no_provider_retry', False):
                raise
            delay = retry_delay(failure, attempt)
            if delay is None or not can_retry():
                raise
            if remaining is not None and await remaining() <= delay + .001:
                raise
            if writer is not None:
                try:
                    writer({'type': 'tool_activity', 'phase': 'start', 'tool': 'model_wait',
                            'wait_seconds': delay,
                            'args': f'Bob is waiting on the model for {delay:g} seconds.'})
                except Exception:
                    pass
            if flush is not None:
                await flush()
            try:
                await sleep(delay)
            finally:
                if writer is not None:
                    try:
                        writer({'type': 'tool_activity', 'phase': 'result', 'tool': 'model_wait',
                                'ok': True, 'preview': 'The model wait ended.'})
                    except Exception:
                        pass
            if flush is not None:
                await flush()
