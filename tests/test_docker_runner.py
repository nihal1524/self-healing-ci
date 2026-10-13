from pathlib import Path

import docker

from core.docker_runner import DockerTestRunner


class FakeImages:
    def __init__(self, cached=False):
        self.cached = cached
        self.build_calls = []
        self.context_files = []

    def get(self, tag):
        if self.cached:
            return object()
        raise docker.errors.ImageNotFound("not cached")

    def build(self, **kwargs):
        context = Path(kwargs["path"])
        dockerfile = context / kwargs["dockerfile"]
        self.build_calls.append((kwargs, dockerfile.read_text()))
        self.context_files.append(
            {
                str(path.relative_to(context)): path.read_text()
                for path in context.rglob("*")
                if path.is_file()
            }
        )
        return object(), iter([{"stream": "prepared\n"}])


class FakeClient:
    def __init__(self, images):
        self.images = images


def test_setup_command_builds_environment_image_without_copying_repo(tmp_path, monkeypatch):
    images = FakeImages()
    monkeypatch.setattr(docker, "from_env", lambda: FakeClient(images))

    runner = DockerTestRunner(
        str(tmp_path),
        image="python:3.8-slim",
        test_command="pytest -q",
        setup_command="pip install pytest numpy",
    )

    assert runner.image.startswith("self-heal-setup:")
    assert len(images.build_calls) == 1
    build_args, dockerfile = images.build_calls[0]
    assert build_args["tag"] == runner.image
    assert dockerfile == (
        "FROM python:3.8-slim\nRUN pip install pytest numpy\n"
    )
    assert "COPY" not in dockerfile
    assert runner.test_command == "pytest -q"


def test_setup_command_reuses_cached_environment_image(tmp_path, monkeypatch):
    images = FakeImages(cached=True)
    monkeypatch.setattr(docker, "from_env", lambda: FakeClient(images))

    runner = DockerTestRunner(
        str(tmp_path),
        image="python:3.8-slim",
        setup_command="pip install pytest numpy",
    )

    assert runner.image.startswith("self-heal-setup:")
    assert images.build_calls == []


def test_setup_image_cache_key_includes_platform(tmp_path, monkeypatch):
    images = FakeImages()
    monkeypatch.setattr(docker, "from_env", lambda: FakeClient(images))

    amd64_runner = DockerTestRunner(
        str(tmp_path), image="python:3.8-slim", setup_command="pip install pytest",
        platform="linux/amd64",
    )
    arm64_runner = DockerTestRunner(
        str(tmp_path), image="python:3.8-slim", setup_command="pip install pytest",
        platform="linux/arm64",
    )

    assert amd64_runner.image != arm64_runner.image
    assert images.build_calls[0][0]["platform"] == "linux/amd64"
    assert images.build_calls[1][0]["platform"] == "linux/arm64"


def test_requirements_are_baked_and_local_project_is_installed_at_runtime(
    tmp_path, monkeypatch
):
    (tmp_path / "requirements.txt").write_text("numpy==1.26.4\n-e .\n")
    (tmp_path / "setup.py").write_text("from setuptools import setup; setup()\n")
    images = FakeImages()
    monkeypatch.setattr(docker, "from_env", lambda: FakeClient(images))

    runner = DockerTestRunner(str(tmp_path), image="python:3.12-slim")

    _, dockerfile = images.build_calls[0]
    assert "COPY requirements.txt /tmp/requirements.txt" in dockerfile
    assert "pip install --no-cache-dir -r /tmp/requirements.txt" in dockerfile
    assert "pip install --no-cache-dir pytest ruff" in dockerfile
    assert images.context_files[0]["requirements.txt"] == "numpy==1.26.4\n"
    assert runner.test_command.startswith("pip install -e . && ")
    assert "pip install -r requirements.txt" not in runner.test_command


def test_custom_test_command_gets_editable_install_for_current_checkout(
    tmp_path, monkeypatch
):
    (tmp_path / "requirements.txt").write_text("requests==2.32.3\n-e .\n")
    images = FakeImages()
    monkeypatch.setattr(docker, "from_env", lambda: FakeClient(images))

    runner = DockerTestRunner(
        str(tmp_path),
        image="python:3.12-slim",
        test_command="python -m pytest -q",
    )

    assert runner.test_command == "pip install -e . && python -m pytest -q"


def test_existing_editable_install_in_custom_command_is_not_duplicated(
    tmp_path, monkeypatch
):
    (tmp_path / "requirements.txt").write_text("-e .\n")
    images = FakeImages()
    monkeypatch.setattr(docker, "from_env", lambda: FakeClient(images))

    runner = DockerTestRunner(
        str(tmp_path),
        image="python:3.12-slim",
        test_command="python -m pip install -e . && python -m pytest -q",
    )

    assert runner.test_command.count("install -e .") == 1


def test_requirements_content_changes_cached_image_tag(tmp_path, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("numpy==1.26.4\n")
    images = FakeImages()
    monkeypatch.setattr(docker, "from_env", lambda: FakeClient(images))

    first = DockerTestRunner(str(tmp_path), image="python:3.12-slim").image
    requirements.write_text("numpy==2.0.0\n")
    second = DockerTestRunner(str(tmp_path), image="python:3.12-slim").image

    assert first != second
    assert len(images.build_calls) == 2


def test_nested_requirements_are_copied_for_cached_install(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("-r requirements/base.txt\n")
    (tmp_path / "requirements").mkdir()
    (tmp_path / "requirements/base.txt").write_text("requests==2.32.3\n")
    images = FakeImages()
    monkeypatch.setattr(docker, "from_env", lambda: FakeClient(images))

    DockerTestRunner(str(tmp_path), image="python:3.12-slim")

    context_files = images.context_files[0]
    assert context_files["requirements.txt"] == "-r requirements/base.txt\n"
    assert context_files["requirements/base.txt"] == "requests==2.32.3\n"