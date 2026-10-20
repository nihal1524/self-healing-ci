from pathlib import Path

from core.context_extractor import extract_failure_context
from core.docker_runner import DockerTestRunner


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_imported_implementation_is_included_without_traceback_frame():
    repo = PROJECT_ROOT / "sample-direct-multifile"
    output = """
FAILED tests/test_calculator.py::test_multiply_returns_product - assert 13 == 42
  File "/app/tests/test_calculator.py", line 5, in test_multiply_returns_product
    assert multiply(6, 7) == 42
"""

    contexts = extract_failure_context(str(repo), output)
    paths = {item["path"] for item in contexts}

    assert "tests/test_calculator.py" in paths
    assert "calculator.py" in paths


def test_collection_traceback_finds_broken_importing_module():
    repo = PROJECT_ROOT / "sample-collection-error"
    output = """
ERROR collecting tests/test_money.py
ImportError while importing test module '/app/tests/test_money.py'.
  File "/app/tests/test_money.py", line 3, in <module>
    from money import round_money
  File "/app/money.py", line 1, in <module>
    from decimal import Decimal, ROUND_HALF_UPP
ImportError: cannot import name 'ROUND_HALF_UPP' from 'decimal'
"""

    contexts = extract_failure_context(str(repo), output)
    paths = {item["path"] for item in contexts}

    assert "tests/test_money.py" in paths
    assert "money.py" in paths


def test_junit_xml_report_is_used_as_a_hybrid_input():
    repo = PROJECT_ROOT / "sample-direct-multifile"
    xml = '''<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite><testcase classname="tests.test_calculator" name="test_multiply_returns_product">
<failure message="assert 13 == 42">File "/app/tax.py", line 6, in round_tax
NameError: ROUND_HALF_UP is not defined</failure></testcase></testsuite></testsuites>'''

    contexts = extract_failure_context(str(repo), xml)
    paths = {item["path"] for item in contexts}

    assert "tests/test_calculator.py" in paths
    assert "tax.py" in paths


def test_docker_runner_generates_junit_xml_report():
    runner = DockerTestRunner(str(PROJECT_ROOT / "sample-direct-multifile"))
    assert "--junitxml=/app/report.xml" in runner.test_command


def test_dotted_import_finds_nested_module_and_parent_package_init(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "fastapi").mkdir()
    (tmp_path / "tests/test_api.py").write_text(
        "from fastapi.encoders import jsonable_encoder\n"
    )
    (tmp_path / "fastapi/__init__.py").write_text("from .encoders import jsonable_encoder\n")
    (tmp_path / "fastapi/encoders.py").write_text(
        "def jsonable_encoder(value):\n    return value\n"
    )

    contexts = extract_failure_context(
        str(tmp_path), "FAILED tests/test_api.py::test_encoder - AssertionError\n"
    )
    paths = {item["path"] for item in contexts}

    assert "tests/test_api.py" in paths
    assert "fastapi/encoders.py" in paths
    assert "fastapi/__init__.py" in paths


def test_imported_package_module_resolves_package_init(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "codec/encoders").mkdir(parents=True)
    (tmp_path / "tests/test_codec.py").write_text(
        "from codec.encoders import encode\n"
    )
    (tmp_path / "codec/__init__.py").write_text("")
    (tmp_path / "codec/encoders/__init__.py").write_text(
        "def encode(value):\n    return value\n"
    )

    contexts = extract_failure_context(
        str(tmp_path), "FAILED tests/test_codec.py::test_encode - AssertionError\n"
    )
    paths = {item["path"] for item in contexts}

    assert "codec/encoders/__init__.py" in paths
    assert "codec/__init__.py" in paths


def test_missing_and_relative_imports_do_not_escape_or_add_files(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_local.py").write_text(
        "from .helpers import local_helper\n"
        "from external_library.module import dependency\n"
        "from missing.package import absent\n"
    )

    contexts = extract_failure_context(
        str(tmp_path), "FAILED tests/test_local.py::test_case - AssertionError\n"
    )
    paths = {item["path"] for item in contexts}

    assert paths == {"tests/test_local.py"}


def _method_repo(tmp_path, definitions, test_imports=""):
    (tmp_path / "pkg").mkdir()
    for name, body in definitions.items():
        (tmp_path / "pkg" / f"{name}.py").write_text(body)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        f"{test_imports}\n\ndef test_x():\n    obj = make()\n    obj.follow(None)\n    obj.assertEqual(1, 1)\n"
    )
    output = '  File "/app/tests/test_x.py", line 5, in test_x\n    obj.follow(None)\n'
    return {c["path"]: c for c in extract_failure_context(str(tmp_path), output)}


def test_method_called_on_object_pulls_in_its_definition(tmp_path):
    contexts = _method_repo(tmp_path, {"resp": "class Resp:\n    def follow(self, url):\n        return url\n"})

    labels = [f["label"] for f in contexts["pkg/resp.py"]["focus"]]
    assert labels == ["Method Resp.follow"]
    assert "assertEqual" not in str(contexts.keys())


def test_method_name_in_imported_module_beats_other_matches(tmp_path):
    defs = {f"m{i}": f"class C{i}:\n    def follow(self):\n        pass\n" for i in range(5)}
    contexts = _method_repo(tmp_path, defs, test_imports="from pkg.m2 import C2")

    assert [f["label"] for f in contexts["pkg/m2.py"]["focus"]] == ["Method C2.follow"]
    assert "pkg/m0.py" not in contexts


def test_generic_method_defined_in_many_places_is_skipped(tmp_path):
    defs = {f"m{i}": f"class C{i}:\n    def follow(self):\n        pass\n" for i in range(5)}
    contexts = _method_repo(tmp_path, defs)

    assert not any(path.startswith("pkg/") for path in contexts)


def test_method_passed_as_reference_pulls_in_its_definition(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "resp.py").write_text("class Resp:\n    def follow(self, url):\n        return url\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_x():\n    r = make()\n    assertRaises(ValueError, r.follow, None)\n"
    )
    output = '  File "/app/tests/test_x.py", line 3, in test_x\n'

    contexts = {c["path"]: c for c in extract_failure_context(str(tmp_path), output)}

    assert [f["label"] for f in contexts["pkg/resp.py"]["focus"]] == ["Method Resp.follow"]
