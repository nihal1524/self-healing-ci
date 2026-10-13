# Self-Healing CI/CD — Phase 2 (LangGraph workflow)

A local CLI prototype that uses LangGraph to orchestrate a target repository's
test, diagnosis, patch, and verification steps. Docker runs tests; the workflow
extracts likely relevant files, asks Claude for a structured patch, applies it
to the local working tree, and reruns tests to verify the change.

## Prerequisites

| Requirement | Purpose | Check |
|---|---|---|
| Python 3.10+ | Runs this CLI | `python3 --version` |
| Docker Desktop (or compatible Docker daemon) running | Runs the target tests in an isolated container | `docker ps` |
| Git and the `patch` utility | Applies generated unified diffs | `git --version`, `patch --version` |
| Anthropic API key | Generates proposed fixes | `ANTHROPIC_API_KEY` must be set |
| LangGraph | Orchestrates workflow nodes and routing | Installed by `requirements.txt` |
| Network access | Installs test/lint tools in the container and calls Anthropic | — |

Auto-detection supports a basic Python/pytest workflow and a Node/npm workflow.
For Python, a `requirements.txt` is optional. For Node, the target must have a
`package.json` with a working `test` script. Override detection with `--image`
and `--test-cmd` when needed.

## Setup

From the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and put your Anthropic key there. In zsh, load it into the current
terminal before running the CLI:

```bash
set -a
source .env
set +a
```

Alternatively, set `ANTHROPIC_API_KEY` directly in the terminal environment.
The Python CLI does not itself load `.env`; in VS Code, enable
`python.terminal.useEnvFile` and open a new terminal, or source the file as
shown above. Never commit `.env` or share its key. `.env.example` is a safe
placeholder template.

Confirm Docker is available:

```bash
docker ps
```

An empty container list is fine; it means no containers are currently running.
If the command errors, start Docker Desktop and retry.

## Try the included samples

The simple calculator sample has an intentional failing addition test:

```bash
python3 heal.py ./sample-failing-repo
```

The business-pricing sample contains more than one intentional issue, including
a missing import and a free-shipping business-rule bug:

```bash
python3 heal.py ./sample-business-repo --max-attempts 3
```

The LLM response depends on the test output and provider/model behavior, so
exact proposed diffs may vary.

### Repeatable repair fixtures

Two additional fixtures have committed `main` (broken baseline) and
`known-good` (passing reference) branches. They are currently checked out on
`main` so the CLI sees the broken version. The known-good branches let you
compare the expected repaired state without relying on the LLM's exact patch.

**Direct failure, import-based context discovery, runtime error, and multi-file
repair:**

```bash
python3 heal.py ./sample-direct-multifile --max-attempts 3
```

The multiplication test fails at an assertion, while a separate tax test
raises a missing-import `NameError`. The implementation module for the
multiplication failure is imported by the test but does not have its own
traceback frame. A complete repair touches both implementation files.

**Pytest collection-time import error:**

```bash
python3 heal.py ./sample-collection-error --max-attempts 2
```

This fails while importing a misspelled Decimal constant during test
collection, before pytest executes the test body.

To reset either fixture after a run, discard its uncommitted changes and remove
ignored test caches/generated patch files, returning to its committed broken
baseline:

```bash
git -C sample-direct-multifile switch main
git -C sample-direct-multifile reset --hard
git -C sample-direct-multifile clean -fdX
git -C sample-collection-error switch main
git -C sample-collection-error reset --hard
git -C sample-collection-error clean -fdX
```

To run the independently committed passing references, switch one fixture to
`known-good` and run pytest from its directory:

```bash
git -C sample-direct-multifile switch known-good
git -C sample-direct-multifile clean -fdX
cd sample-direct-multifile && python -m pytest -q
```

Switch back to `main` before using `heal.py`. The lightweight workflow tests
also cover initial-pass/no-LLM, final verification exhaustion, and a patch
application failure without making Docker or API calls. A separate patch
applier test exercises a stale diff against a temporary working tree.

## Run against your own repository

Use the actual local path to your target repo (the paths below are examples):

```bash
python3 heal.py /path/to/your/repo
python3 heal.py /path/to/your/repo --max-attempts 5
python3 heal.py /path/to/your/repo \
  --image python:3.12-slim \
  --test-cmd "(ruff check . || true) && python -m pytest -v --junitxml=/app/report.xml"
```

For Python repositories with a root `requirements.txt`, the runner automatically
builds a reusable dependency image. The image cache key includes the base image
and contents of the root and included requirements files, so edits to source
reuse the image while dependency changes build a new one. Third-party packages,
pytest, and Ruff are installed into that image. Local editable requirements
such as `-e .` are omitted from the image and installed from the live mounted
checkout before tests. Each test attempt still uses a fresh container.

For additional OS packages or specialized environment setup, pass `--setup-cmd`.
That command runs during Docker image build, so use it for environment
dependencies—not commands that need the checked-out source tree. The image is
tagged from the base image and setup command. Tests still run in a fresh
container with the repository mounted at `/app`:

```bash
python3 heal.py /path/to/your/repo \
  --image python:3.12-slim \
  --setup-cmd "apt-get update && apt-get install -y --no-install-recommends build-essential && pip install pytest numpy" \
  --test-cmd "python -m pytest -v --junitxml=/app/report.xml"
```

For legacy projects whose pinned dependencies only publish wheels for another
CPU architecture, select a Docker target platform (for example,
`--platform linux/amd64` on Apple Silicon). The platform is used for both image
build and test containers and is part of the prepared-image cache key. Cross-
architecture execution requires Docker emulation and can be slower.

The first image build still downloads and installs dependencies. Later attempts
and runs reuse the image when the base image and dependency inputs are
unchanged. Project-source compilation (for example, building native extensions
from the mounted pandas checkout) is source-dependent and remains a separate
build step; it is not performed in the dependency image.

For Python projects without a root `requirements.txt`, the default test command
installs pytest and Ruff at runtime. With a root `requirements.txt`, those
dependencies are installed in the prepared image, and the test command avoids
reinstalling them. The runner removes stale Python `__pycache__` directories,
runs `ruff check .` in check-only mode, then runs `python -m pytest -v`.
Clearing bytecode avoids accidentally testing stale code after a quick patch or
Git branch switch. Ruff does not auto-edit target files.
Its nonzero status is currently ignored so pytest still runs; therefore, a
“Tests passed!” result means pytest passed, not necessarily that Ruff reported
no lint issues. Node projects use `npm install` and `npm test` by default.

`--max-attempts` means the maximum number of patch-generation/application
attempts. The CLI performs up to one additional test run to verify the last
patch. For example, `--max-attempts 3` allows up to three fixes plus a final
verification run. A failed patch application stops the run; the CLI does not
automatically roll back already-applied changes.

## Pipeline components

- **Docker execution** — `core/docker_runner.py` mounts the target repo
  read-write at `/app`, runs its test command, captures logs/exit status, and
  removes the temporary container.
- **Failure context** — `core/context_extractor.py` uses lightweight regexes
  over pytest output, Python tracebacks, and JavaScript stack traces, then reads
  matching files from the local repo.
- **LLM contract/call** — `core/models.py` defines Pydantic `PatchPlan` and
  `ProposedPatch` models. `core/patch_generator.py` sends the context to Claude
  using Anthropic tool calling and validates the returned data as a `PatchPlan`.
- **Patch application** — `core/patch_applier.py` writes each unified diff to
  an `attemptN_i.patch` file inside the target repo and tries `git apply`, then
  falls back to `patch -p1`.
- **Workflow state and routing** — `core/workflow.py` defines a LangGraph
  `StateGraph` with state for the repo, test output, contexts, patch plan, and
  fix count. Conditional edges route success to the end, test failures through
  context/generation/application, and final failed verification to failure.
- **CLI entry point** — `heal.py` remains the local command-line interface. It
  parses options and invokes the compiled graph, so local sample testing stays
  available through the same command.
- **Retry and verification** — the graph allows the configured number of
  successful patch applications, then performs one additional test run to
  verify the last patch. Missing context or failed patch application stops the
  graph without continuing to another test run.

## Review changes carefully

The target repository is mounted read-write. LLM patch application can modify
its local files; Ruff runs in check-only mode and does not make edits. Run the
pipeline on a clean Git working tree when possible, then review changes before
committing:

```bash
git -C /path/to/your/repo status
git -C /path/to/your/repo diff
```

Generated patch files are left in the target repo. The pipeline does not commit
changes or automatically roll them back. Do not run it unattended against a
repository whose edits you cannot safely review or restore.

## Known limitations

- File discovery is regex-based, not a full pytest/Jest report parser; some
  unusual failure formats may not yield usable context.
- Ruff is check-only in the default command. Its nonzero status is ignored so
  pytest can still run; lint findings alone do not trigger a repair request.
- Patch application can leave partial changes if one patch in a multi-patch
  plan applies but a later patch fails.
- LangGraph state is held in memory for the duration of one CLI invocation;
  checkpointing and pause/resume are not configured.
- Langfuse tracing/observability, CI webhook integration, patch sandboxing, and
  automated rollback are not integrated yet.

The graph routing can be tested locally without Docker or an API key:

```bash
python -m pytest -q tests
```

## Troubleshooting

- **Docker API/daemon connection error:** start Docker Desktop and confirm
  `docker ps` works.
- **`ANTHROPIC_API_KEY` missing:** source `.env` in the same terminal session
  (`set -a; source .env; set +a`) or set the variable directly.
- **Python `ModuleNotFoundError` for a repo module:** use `python -m pytest`
  in the test command and ensure the module is in the mounted repo.
- **No files found from test output:** check that pytest actually collected and
  ran tests; collection errors or zero collected tests may not match the
  context extractor's failure patterns.
- **Patch application fails:** inspect the emitted patch file and target repo
  diff; the diff paths/context may not match the current working tree.
