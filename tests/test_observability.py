import types

import pytest

from core import observability as ob
from tests.test_patch_generator import GOOD, _fake_anthropic


class FakeObservation:
    def __init__(self, log, name, kwargs):
        self.updates = []
        log.append((name, kwargs, self.updates))

    def update(self, **kwargs):
        self.updates.append(kwargs)


class FakeClient:
    def __init__(self):
        self.log = []

    def start_as_current_observation(self, name, as_type, **kwargs):
        import contextlib

        @contextlib.contextmanager
        def manager():
            yield FakeObservation(self.log, name, {"as_type": as_type, **kwargs})

        return manager()


def test_everything_is_a_noop_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    with ob.run_trace("run", tags=["t"]) as run:
        with ob.observe("step") as step:
            step.update(output={"a": 1})
        run.update(output={})
    ob.flush()


def test_tracing_failures_never_break_the_run(monkeypatch):
    class Broken:
        def start_as_current_observation(self, **_):
            raise RuntimeError("langfuse down")

        def flush(self):
            raise RuntimeError("langfuse down")

    monkeypatch.setattr(ob, "_client", lambda: Broken())

    with ob.observe("step") as step:
        step.update(output={})
    ob.flush()


def test_errors_inside_a_span_still_propagate(monkeypatch):
    monkeypatch.setattr(ob, "_client", lambda: FakeClient())

    with pytest.raises(ValueError):
        with ob.observe("step"):
            raise ValueError("boom")


def test_traced_node_returns_result_and_records_metadata_only(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(ob, "_client", lambda: client)
    node = ob.traced("run-tests", lambda state, result: {"status": result["status"]})(
        lambda state: {"status": "patch_error", "error": "no match", "test_output": "HUGE"}
    )

    assert node({})["status"] == "patch_error"
    (name, _, updates), = client.log
    assert name == "run-tests"
    assert updates[0]["output"] == {"status": "patch_error"}
    assert updates[0]["level"] == "ERROR" and updates[0]["status_message"] == "no match"


def test_failing_summary_does_not_break_the_node(monkeypatch):
    monkeypatch.setattr(ob, "_client", lambda: FakeClient())
    node = ob.traced("x", lambda state, result: 1 / 0)(lambda state: {"status": "ok"})

    assert node({}) == {"status": "ok"}


def test_generation_records_usage_and_edits_but_not_the_prompt(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(ob, "_client", lambda: client)
    pg, _ = _fake_anthropic(monkeypatch, [GOOD])

    pg.generate_patch("SECRET TEST OUTPUT", [{"path": "a.py", "content": "SECRET SOURCE"}], model="m")

    (name, kwargs, updates), = client.log
    assert name == "patch-generation" and kwargs["as_type"] == "generation" and kwargs["model"] == "m"
    assert "input" not in kwargs and "SECRET" not in str(client.log)
    assert updates[0]["output"]["patches"][0]["old"] == "x = 1"
    assert updates[0]["usage_details"] == {
        "input": 10, "output": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }


def test_startup_report_names_exactly_what_is_missing(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-abcdefgh")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    assert ob.startup_report() == "[langfuse] DISABLED - not set or empty: LANGFUSE_SECRET_KEY"


def test_startup_report_flags_failed_auth_and_default_region(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-abcdefgh")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-secret")
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    class BadAuth:
        def auth_check(self):
            raise RuntimeError("no projects found")

    monkeypatch.setattr(ob, "_client", lambda: BadAuth())
    report = ob.startup_report()

    assert "auth check FAILED: no projects found" in report
    assert "SDK default - set LANGFUSE_BASE_URL" in report
    assert "sk-lf-secret" not in report  # the secret is never printed


def test_startup_report_ok(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-abcdefgh")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-secret")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://jp.cloud.langfuse.com")
    monkeypatch.setattr(ob, "_client", lambda: types.SimpleNamespace(auth_check=lambda: True))

    assert ob.startup_report().endswith("host=https://jp.cloud.langfuse.com | public key pk-lf-abcd... | auth check: ok")


def test_swallowed_errors_are_printed_once(monkeypatch, capsys):
    class Broken:
        def start_as_current_observation(self, **_):
            raise RuntimeError("langfuse down")

    monkeypatch.setattr(ob, "_client", lambda: Broken())
    ob._warned.clear()

    for _ in range(3):
        with ob.observe("step"):
            pass

    assert capsys.readouterr().out.count("[langfuse] warning: could not start a span: langfuse down") == 1


def _span(scope_name, **attributes):
    return types.SimpleNamespace(instrumentation_scope=types.SimpleNamespace(name=scope_name), attributes=attributes)


def test_export_filter_drops_the_anthropic_sdks_own_spans_but_keeps_ours():
    assert ob._export_filter(_span("com.anthropic.sdk.python")) is False
    assert ob._export_filter(_span("langfuse-sdk")) is True          # our generations and spans


def test_export_filter_still_applies_langfuses_default_rule_to_everything_else():
    assert ob._export_filter(_span("some.random.library")) is False  # not a langfuse/genai/known-LLM span
    assert ob._export_filter(_span("some.random.library", **{"gen_ai.system": "x"})) is True
