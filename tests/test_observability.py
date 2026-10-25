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
    assert updates[0]["usage_details"] == {"input": 10, "output": 5}
