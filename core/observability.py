"""
Langfuse tracing -- metadata only (statuses, counts, file paths, token usage and
the model's old/new edits; never prompts, file contents or test output).

Everything here is a no-op unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are
set (LANGFUSE_BASE_URL selects the region), and it never raises: observability
must not be able to break a repair run.
"""

import contextlib
import functools
import os

ERROR_STATUSES = {
    "runner_error", "context_error", "no_context", "llm_error", "patch_error", "verification_failed",
}


def _client():
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    try:
        from langfuse import get_client

        return get_client()
    except Exception:
        return None


class Observation:
    """Handle yielded by observe(); update() is safe even when tracing is off."""

    def __init__(self, observation=None):
        self._observation = observation

    def update(self, **kwargs):
        if self._observation is None:
            return
        try:
            self._observation.update(**kwargs)
        except Exception:
            pass


@contextlib.contextmanager
def _guarded(factory):
    """Enter factory() if it works; exceptions from the body still propagate."""
    try:
        manager = factory()
        value = manager.__enter__()
    except Exception:
        yield None
        return
    try:
        yield value
    except BaseException as exc:
        try:
            manager.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            pass
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            pass


@contextlib.contextmanager
def observe(name: str, as_type: str = "span", **kwargs):
    client = _client()
    factory = (lambda: client.start_as_current_observation(name=name, as_type=as_type, **kwargs)) if client else None
    if factory is None:
        yield Observation()
        return
    with _guarded(factory) as observation:
        yield Observation(observation)


@contextlib.contextmanager
def run_trace(name: str, tags: list[str] | None = None, metadata: dict | None = None):
    """Root of one heal.py run; tags/metadata apply to every observation inside it."""
    if _client() is None:
        yield Observation()
        return
    from langfuse import propagate_attributes

    attributes = {k: str(v)[:200] for k, v in (metadata or {}).items()}
    with _guarded(lambda: propagate_attributes(trace_name=name, tags=tags or None, metadata=attributes or None)):
        with observe(name) as run:
            yield run


def traced(name: str, summarize):
    """Wrap a workflow node in a span; summarize(state, result) -> metadata-only output."""

    def decorator(node):
        @functools.wraps(node)
        def wrapper(state):
            with observe(name) as span:
                result = node(state)
                try:
                    status = result.get("status", "")
                    span.update(
                        output=summarize(state, result),
                        level="ERROR" if status in ERROR_STATUSES else "WARNING" if status == "patch_retry" else "DEFAULT",
                        status_message=result.get("error") or None,
                    )
                except Exception:
                    pass
                return result

        return wrapper

    return decorator


def flush():
    client = _client()
    if client is not None:
        try:
            client.flush()
        except Exception:
            pass
