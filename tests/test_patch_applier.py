from core.patch_applier import apply_patch


def test_stale_patch_is_rejected_without_changing_target(tmp_path):
    target = tmp_path / "calculator.py"
    original = "def add(a, b):\n    return 7\n"
    target.write_text(original)
    stale_diff = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return 1
+    return 2
"""

    result = apply_patch(str(tmp_path), stale_diff, patch_name="stale.patch")

    assert result["success"] is False
    assert target.read_text() == original
    assert (tmp_path / "stale.patch").is_file()
