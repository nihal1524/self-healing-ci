"""
Langfuse tracing -- metadata only (statuses, counts, file paths, token usage and
the model's old/new edits; never prompts, file contents or test output).

Everything here is a no-op unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are
set (LANGFUSE_BASE_URL selects the region), and it never raises: observability
must not be able to break a repair run. It is not silent either: startup_report()
says whether tracing is on and whether the keys/region work, every swallowed error
is printed once as "[langfuse] warning: ...", and trace_url() gives a link to the run.
"""

import contextlib
import functools
import os

ERROR_STATUSES = {
    "runner_error", "context_error", "no_context", "llm_error", "patch_error", "verification_failed",
}
DEFAULT_HOST = "https://cloud.langfuse.com"

_warned: set[str] = set()
_trace_url: str | None = None
_initialized = False
ANTHROPIC_SDK_SCOPE = "com.anthropic."  # newer anthropic SDKs trace every request on their own


def _warn(message: str) -> None:
    """Print each distinct Langfuse problem once; never raise."""
    if message not in _warned:
        _warned.add(message)
        print(f"[langfuse] warning: {message}", flush=True)


def _export_filter(span) -> bool:
    """Langfuse's default export rule, minus the Anthropic SDK's own spans.

    patch_generator already records each model call (with cache usage and the model's edit) as
    "patch-generation"; exporting the SDK's span too would show one request as two generations and
    double-count its tokens and cost in the trace totals.
    """
    from langfuse import is_default_export_span

    scope = getattr(span, "instrumentation_scope", None)
    if scope is not None and (scope.name or "").startswith(ANTHROPIC_SDK_SCOPE):
        return False
    return is_default_export_span(span)


def _client():
    global _initialized
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    try:
        from langfuse import Langfuse, get_client

        if not _initialized:
            Langfuse(should_export_span=_export_filter)  # reads the LANGFUSE_* env vars; get_client() returns it
            _initialized = True
        return get_client()
    except Exception as exc:
        _warn(f"could not create the client: {exc}")
        return None


def startup_report() -> str:
    """One line saying whether tracing is on, where it goes, and whether the keys work (blocking, once)."""
    public = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")
    missing = [name for name, value in (("LANGFUSE_PUBLIC_KEY", public), ("LANGFUSE_SECRET_KEY", secret)) if not value]
    if missing:
        return f"[langfuse] DISABLED - not set or empty: {', '.join(missing)}"
    if os.environ.get("LANGFUSE_TRACING_ENABLED", "").lower() == "false":
        return "[langfuse] DISABLED - LANGFUSE_TRACING_ENABLED=false"
    shown_host = host or f"{DEFAULT_HOST} (SDK default - set LANGFUSE_BASE_URL if your project is in another region)"
    prefix = f"[langfuse] host={shown_host} | public key {public[:10]}..."
    client = _client()
    if client is None:
        return f"{prefix} | DISABLED - could not create the client"
    try:
        ok = client.auth_check()
    except Exception as exc:
        return f"{prefix} | auth check FAILED: {exc}. Check the keys and that the host matches the project's region."
    return f"{prefix} | auth check: {'ok' if ok else 'FAILED'}"


def trace_url() -> str | None:
    """Link to this run's trace in the Langfuse UI (set while run_trace is active)."""
    return _trace_url


class Observation:
    """Handle yielded by observe(); update() is safe even when tracing is off."""

    def __init__(self, observation=None):
        self._observation = observation

    def update(self, **kwargs):
        if self._observation is None:
            return
        try:
            self._observation.update(**kwargs)
        except Exception as exc:
            _warn(f"span update failed: {exc}")


@contextlib.contextmanager
def _guarded(factory):
    """Enter factory() if it works; exceptions from the body still propagate."""
    try:
        manager = factory()
        value = manager.__enter__()
    except Exception as exc:
        _warn(f"could not start a span: {exc}")
        yield None
        return
    try:
        yield value
    except BaseException as exc:
        try:
            manager.__exit__(type(exc), exc, exc.__traceback__)
        except Exception as inner:
            _warn(f"could not end a span: {inner}")
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception as exc:
            _warn(f"could not end a span: {exc}")


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
    global _trace_url
    client = _client()
    if client is None:
        yield Observation()
        return
    from langfuse import propagate_attributes

    attributes = {k: str(v)[:200] for k, v in (metadata or {}).items()}
    with _guarded(lambda: propagate_attributes(trace_name=name, tags=tags or None, metadata=attributes or None)):
        with observe(name) as run:
            try:
                trace_id = client.get_current_trace_id()
                _trace_url = client.get_trace_url(trace_id=trace_id) if trace_id else None
            except Exception as exc:
                _warn(f"could not build the trace link: {exc}")
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
                except Exception as exc:
                    _warn(f"could not summarize the '{name}' step: {exc}")
                return result

        return wrapper

    return decorator


def flush():
    client = _client()
    if client is not None:
        try:
            client.flush()
        except Exception as exc:
            _warn(f"flush failed (traces may not have been sent): {exc}")
