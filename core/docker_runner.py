"""
A. Mount Path in Docker SDK (docker-py)

Wraps docker-py to:
  1. Detect a sensible base image + test command for the target repo
     (Python/pytest vs Node/npm), unless the user overrides them.
  2. Mount the local repo path read-write into the container at /app.
  3. Run the test command, capture stdout+stderr and the exit code.
  4. Clean up the container afterwards.

Requires a running Docker daemon (Docker Desktop / colima / etc.) reachable
via the default socket that `docker` (docker-py) picks up from the
environment, same as the `docker` CLI.
"""

import hashlib
import os
import re
import tempfile
import docker
from docker.errors import ImageNotFound, APIError


class DockerTestRunner:
    def __init__(
        self,
        repo_path: str,
        image: str | None = None,
        test_command: str | None = None,
        setup_command: str | None = None,
        platform: str | None = None,
    ):
        self.repo_path = os.path.abspath(repo_path)
        if not os.path.isdir(self.repo_path):
            raise ValueError(f"Not a directory: {self.repo_path}")

        self.client = docker.from_env()
        self.platform = platform
        self.base_image = image or self._detect_image()
        self._requirements_files, self._install_editable_project = (
            self._discover_requirements() if not self._is_node_project() else ([], False)
        )
        self._requirements_baked = False
        self.image = self.base_image
        if self._requirements_files:
            self.image = self._prepare_requirements_image()
            self._requirements_baked = True
        if setup_command:
            self.image = self._prepare_image(setup_command, base_image=self.image)

        self.test_command = test_command or self._detect_test_command()
        if self._install_editable_project:
            self.test_command = self._ensure_editable_install(self.test_command)

    @staticmethod
    def _ensure_editable_install(test_command: str) -> str:
        """Install the live mounted project unless the command already does."""
        for command in re.split(r"[;&|]+", test_command):
            has_pip_install = re.search(
                r"(?:^|\s)(?:python(?:\d(?:\.\d+)?)?\s+-m\s+)?pip(?:3)?\s+install\b",
                command,
            )
            if has_pip_install and re.search(r"(?:^|\s)-e(?:\s|=)", command):
                return test_command
        return f"pip install -e . && {test_command}"

    def _discover_requirements(self) -> tuple[list[str], bool]:
        """Find requirements files and detect a local editable project install."""
        root = os.path.join(self.repo_path, "requirements.txt")
        if not os.path.isfile(root):
            return [], False

        found: set[str] = set()
        visiting: set[str] = set()
        editable_project = False

        def visit(path: str) -> None:
            nonlocal editable_project
            path = os.path.abspath(path)
            if os.path.commonpath([self.repo_path, path]) != self.repo_path:
                raise ValueError(f"Requirements include escapes repository: {path}")
            if path in visiting or path in found:
                return
            if not os.path.isfile(path):
                raise ValueError(f"Included requirements file not found: {path}")
            visiting.add(path)
            found.add(path)
            with open(path, encoding="utf-8") as file:
                for line in file:
                    stripped = line.strip()
                    editable = re.match(r"^(?:-e|--editable(?:=|\s+))\s*(.+?)\s*$", stripped)
                    if editable and editable.group(1).strip("\"'") in {".", "./"}:
                        editable_project = True
                        continue
                    include = re.match(r"^(?:-r|--requirement|-c|--constraint)(?:=|\s+)(.+?)\s*$", stripped)
                    if include:
                        relative = include.group(1).strip("\"'")
                        visit(os.path.join(os.path.dirname(path), relative))
            visiting.remove(path)

        visit(root)
        return sorted(found), editable_project

    def _prepare_requirements_image(self) -> str:
        """Bake third-party requirements into a cached image, not repo source."""
        files = self._requirements_files
        digest = hashlib.sha256(
            f"{self.base_image}\0{self.platform or ''}".encode()
        )
        for path in files:
            relative = os.path.relpath(path, self.repo_path)
            with open(path, "rb") as file:
                content = file.read()
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")
        image_tag = f"self-heal-setup:{digest.hexdigest()[:12]}"
        try:
            self.client.images.get(image_tag)
            print(f"[docker] Reusing requirements image {image_tag}")
            return image_tag
        except ImageNotFound:
            pass

        print(f"[docker] Building requirements image {image_tag} (first run or dependency change)...")
        with tempfile.TemporaryDirectory(prefix="self-heal-image-") as context:
            for path in files:
                relative = os.path.relpath(path, self.repo_path)
                destination = os.path.join(context, relative)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with open(path, encoding="utf-8") as source, open(
                    destination, "w", encoding="utf-8"
                ) as target:
                    for line in source:
                        if re.match(
                            r"^\s*(?:-e|--editable(?:=|\s+))\s*[\"']?\.?/?[\"']?\s*(?:#.*)?$",
                            line,
                        ):
                            continue
                        target.write(line)

            dockerfile = os.path.join(context, "Dockerfile")
            copies = "".join(
                f"COPY {os.path.relpath(path, self.repo_path)} /tmp/{os.path.relpath(path, self.repo_path)}\n"
                for path in files
            )
            with open(dockerfile, "w", encoding="utf-8") as file:
                file.write(
                    f"FROM {self.base_image}\n"
                    f"{copies}"
                    "RUN pip install --no-cache-dir -r /tmp/requirements.txt "
                    "&& pip install --no-cache-dir pytest ruff\n"
                )
            _, build_logs = self.client.images.build(
                path=context,
                dockerfile="Dockerfile",
                tag=image_tag,
                rm=True,
                platform=self.platform,
            )
            for item in build_logs:
                message = item.get("stream") or item.get("error")
                if message:
                    print(message, end="" if message.endswith("\n") else "\n")
        return image_tag

    def _prepare_image(self, setup_command: str, base_image: str | None = None) -> str:
        """Build and cache an environment image; keep repo source out of it."""
        base_image = base_image or self.base_image
        fingerprint = hashlib.sha256(
            f"{base_image}\0{self.platform or ''}\0{setup_command}".encode()
        ).hexdigest()[:12]
        image_tag = f"self-heal-setup:{fingerprint}"
        try:
            self.client.images.get(image_tag)
            print(f"[docker] Reusing prepared image {image_tag}")
            return image_tag
        except ImageNotFound:
            pass

        print(f"[docker] Building prepared image {image_tag} (first run only)...")
        with tempfile.TemporaryDirectory(prefix="self-heal-image-") as context:
            dockerfile = os.path.join(context, "Dockerfile")
            with open(dockerfile, "w") as file:
                file.write(f"FROM {base_image}\nRUN {setup_command}\n")
            _, build_logs = self.client.images.build(
                path=context,
                dockerfile="Dockerfile",
                tag=image_tag,
                rm=True,
                platform=self.platform,
            )
            for item in build_logs:
                message = item.get("stream") or item.get("error")
                if message:
                    print(message, end="" if message.endswith("\n") else "\n")
        return image_tag

    # ---- detection helpers -------------------------------------------------

    def _is_node_project(self) -> bool:
        return os.path.exists(os.path.join(self.repo_path, "package.json"))

    def _detect_image(self) -> str:
        return "node:20-slim" if self._is_node_project() else "python:3.11-slim"

    def _detect_test_command(self) -> str:
        if self._is_node_project():
            return "npm install --no-audit --no-fund && npm test"

        install = "" if self._requirements_baked else "pip install --no-cache-dir pytest ruff && "
        return (
            f"{install}"
            "find . -type d -name __pycache__ -prune -exec rm -rf {} + && "
            "rm -f /app/report.xml && "
            "(ruff check . || true) && python -m pytest -v --junitxml=/app/report.xml"
        )

    # ---- main entrypoint -----------------------------------------------

    def run(self) -> dict:
        """Run the test command inside a fresh container. Returns:
        {"success": bool, "output": str, "exit_code": int}
        """
        return self.run_command(self.test_command)

    def run_command(self, command: str) -> dict:
        """Run any shell command in a fresh container (same result shape as run)."""
        try:
            container = self.client.containers.run(
                self.image,
                command=["sh", "-c", command],
                volumes={self.repo_path: {"bind": "/app", "mode": "rw"}},
                working_dir="/app",
                detach=True,
                network_mode="bridge",  # needs network for pip/npm install
                platform=self.platform,
            )
        except ImageNotFound:
            print(f"[docker] Image {self.image} not found locally, pulling...")
            self.client.images.pull(self.image)
            return self.run_command(command)
        except APIError as e:
            return {"success": False, "output": f"Docker API error: {e}", "exit_code": -1}

        try:
            wait_result = container.wait()
            exit_code = wait_result.get("StatusCode", 1)
            logs = container.logs().decode("utf-8", errors="replace")
        finally:
            try:
                container.remove(force=True)
            except APIError:
                pass

        return {"success": exit_code == 0, "output": logs, "exit_code": exit_code}
