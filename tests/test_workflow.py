from collections import deque
from pathlib import Path

from core.context_extractor import extract_failure_context
from core.models import PatchPlan, ProposedPatch
from core.workflow import run_workflow

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeRunner:
    def __init__(self, results):
        self.results = deque(results)
        self.calls = 0

    def run(self):
        self.calls += 1
        return self.results.popleft()


def failed(output="test failure"):
    return {"success": False, "output": output, "exit_code": 1}


def passed():
    return {"success": True, "output": "5 passed", "exit_code": 0}


def sample_plan():
    return PatchPlan(
        root_cause="The implementation is incorrect.",
        patches=[
            ProposedPatch(
                file_path="calculator.py",
                explanation="Correct the operation.",
                unified_diff="--- a/calculator.py\n+++ b/calculator.py\n",
            )
        ],
    )


def run_with(runner, **kwargs):
    return run_workflow(
        runner=runner,
        repo_path="/repo",
        max_attempts=kwargs.pop("max_attempts", 2),
        model="test-model",
        **kwargs,
    )


def test_initial_passing_tests_finish_without_repair():
    runner = FakeRunner([passed()])
    generated = []

    state = run_with(
        runner,
        patch_generator=lambda *args, **kwargs: generated.append(True),
    )

    assert state["status"] == "success"
    assert runner.calls == 1
    assert generated == []


def test_applied_fix_is_followed_by_test_verification():
    runner = FakeRunner([failed(), passed()])
    applied = []

    state = run_with(
        runner,
        context_extractor=lambda *_: [{"path": "calculator.py", "content": "..."}],
        patch_generator=lambda *args, **kwargs: sample_plan(),
        patch_applier=lambda *args, **kwargs: applied.append(kwargs["patch_name"])
        or {"success": True, "method": "fake", "patch_file": "fake.patch"},
        max_attempts=1,
    )

    assert state["status"] == "success"
    assert state["fixes_used"] == 1
    assert runner.calls == 2
    assert applied == ["attempt1_1.patch"]


def test_final_failed_verification_does_not_request_an_extra_patch():
    runner = FakeRunner([failed("first"), failed("verify")])
    generated = []

    state = run_with(
        runner,
        context_extractor=lambda *_: [{"path": "calculator.py", "content": "..."}],
        patch_generator=lambda *args, **kwargs: generated.append(True) or sample_plan(),
        patch_applier=lambda *args, **kwargs: {
            "success": True,
            "method": "fake",
            "patch_file": "fake.patch",
        },
        max_attempts=1,
    )

    assert state["status"] == "verification_failed"
    assert state["fixes_used"] == 1
    assert runner.calls == 2
    assert generated == [True]


def test_missing_context_stops_before_model_call():
    runner = FakeRunner([failed()])
    generated = []

    state = run_with(
        runner,
        context_extractor=lambda *_: [],
        patch_generator=lambda *args, **kwargs: generated.append(True),
    )

    assert state["status"] == "no_context"
    assert runner.calls == 1
    assert generated == []


def test_patch_application_failure_stops_without_retrying_tests():
    runner = FakeRunner([failed()])

    state = run_with(
        runner,
        context_extractor=lambda *_: [{"path": "calculator.py", "content": "..."}],
        patch_generator=lambda *args, **kwargs: sample_plan(),
        patch_applier=lambda *args, **kwargs: {
            "success": False,
            "output": "diff did not apply",
        },
    )

    assert state["status"] == "patch_error"
    assert runner.calls == 1


def test_business_repo_multi_file_context_and_fix_loop():
    repo = PROJECT_ROOT / "sample-business-repo"
    output = """
FAILED tests/test_pricing.py::test_free_shipping_uses_pre_discount_subtotal - AssertionError

=================================== FAILURES ===================================
__________________ test_free_shipping_uses_pre_discount_subtotal __________________

    def test_free_shipping_uses_pre_discount_subtotal():
        items = [OrderItem("Backpack", Decimal("55.00"), 1)]

        result = calculate_order(
            items,
            discount_percent=Decimal(10),
            tax_rate=Decimal("0.10"),
            free_shipping_threshold=Decimal("50.00"),
        )

        assert result["subtotal"] == Decimal("55.00")
        assert result["discount"] == Decimal("5.50")
        assert result["tax"] == Decimal("4.95")
        assert result["shipping"] == Decimal("0.00")
        assert result["total"] == Decimal("54.45")

    E       assert Decimal('5.00') == Decimal('0.00')
    E        +  where Decimal('5.00') = <Decimal('5.00')>

pricing.py:53: in calculate_order
    shipping = (
        Decimal(0)
        if subtotal >= free_shipping_threshold
        else shipping_fee
    )
"""

    contexts = extract_failure_context(str(repo), output)
    paths = {item["path"] for item in contexts}
    assert "pricing.py" in paths
    assert "tests/test_pricing.py" in paths

    complex_plan = PatchPlan(
        root_cause="The shipping rule incorrectly checks the discounted subtotal and the pricing module is missing the Decimal rounding import.",
        patches=[
            ProposedPatch(
                file_path="pricing.py",
                explanation="Use pre-discount subtotal for free shipping and keep Decimal rounding consistent.",
                unified_diff="--- a/pricing.py\n+++ b/pricing.py\n@@\n-    shipping = (\n-        Decimal(0)\n-        if subtotal >= free_shipping_threshold\n-        else shipping_fee\n-    )\n+    shipping = (\n+        Decimal(0)\n+        if subtotal >= free_shipping_threshold\n+        else shipping_fee\n+    )\n",
            ),
            ProposedPatch(
                file_path="tests/test_pricing.py",
                explanation="Include a richer assertion context for the shipping regression.",
                unified_diff="--- a/tests/test_pricing.py\n+++ b/tests/test_pricing.py\n@@\n-    assert result[\"shipping\"] == Decimal(\"0.00\")\n+    assert result[\"shipping\"] == Decimal(\"0.00\")\n",
            ),
        ],
    )

    runner = FakeRunner([failed(output), passed()])
    state = run_with(
        runner,
        context_extractor=lambda *_: [
            {"path": "pricing.py", "content": "..."},
            {"path": "tests/test_pricing.py", "content": "..."},
        ],
        patch_generator=lambda *args, **kwargs: complex_plan,
        patch_applier=lambda *args, **kwargs: {"success": True, "method": "fake", "patch_file": "fake.patch"},
        max_attempts=2,
    )

    assert state["status"] == "success"
    assert state["fixes_used"] == 1
    assert runner.calls == 2
