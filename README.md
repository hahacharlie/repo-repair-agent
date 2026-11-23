# Repository Repair Agent

This tool tries to fix a repository that does not run. It tests repair changes
on a temporary copy, so it does not edit your original source files.

## Minimum requirements

- Python 3.10 or newer
- An ASU Voyager API key for AI repairs
- Docker

Docker is used for non-Python repositories, repositories with a Dockerfile, or
  repositories that need another Python version.

## Set up the project

Open a terminal in the `repo-repair-agent` folder and run:

```console
python -m pip install -r requirements.txt
```

These commands work on Windows, macOS, and Linux. If your system uses
`python3`, use it instead of `python` in the setup command.

## Add your API key

Get a key from the [ASU Voyager portal](https://voyager.rc.asu.edu):

1. Connect to the ASU network or ASU VPN.
2. Sign in with your ASURITE account.
3. Open your profile and select **Keys**.
4. Create and copy an API key.

The first AI repair creates a `.env` file in the `repo-repair-agent` folder.
Open it and add your key:

```text
ASU_AIR_API_KEY=your_key_here
```

Save the file, then run the repair command again. Git ignores `.env`, so the
key is not pushed. Do not share this file.

## Add a repository

Put the repository inside:

```text
input/broken_repos/
```

For example:

```text
input/broken_repos/my-repository/
```

You only need to give the folder name when running the agent.

You can also give the full path when the repository is stored somewhere else.

## Run a repair

```console
uv run repo-repair-agent my-repository
```

### How the AI agents work

The tool will:

1. Make a temporary copy of the repository.
2. Find the language, run command, and tests.
3. Run the repository and collect the error.
4. Ask the Fixer for one action at a time.
5. Ask the Judge to review each change in the proposed and stakeholder modes.
6. Run the repository again to check the repair.

The Fixer has two groups of actions:

| Group | Action | What it does | Judge review |
| --- | --- | --- | --- |
| Look | `read` | Read lines of one file | No |
| Look | `search` | Find text in the repository, a folder, or one file | No |
| Look | `inspect` | Run a read-only shell command in the sandbox | No |
| Repair | `run` | Run a shell command that prepares the sandbox | Yes |
| Repair | `edit` | Change files, or add a file that the project lacks | Yes |
| Repair | `install` | Add one language package | No |
| Repair | `commands` | Replace the build and test commands | Yes |
| Stop | `give_up` | Stop the repair | No |

- A look action does not use a repair round. It has its own limit: three for
  each repair round. After that limit, each look action uses one round.
- `inspect` runs as a separate user that cannot write the repository. The
  build, the tests, and `run` actions use the build user.
- The Fixer can send the same action again after the failure changed.
- `inspect`, `run`, and `commands` work only in the Docker sandbox.
- `run` can install system packages as root with `apt-get`, fetch a missing
  dependency, or restore a file that the project lacks.
- No action can change the tests. The tool restores each changed test file
  from the original repository before it runs the tests.
- An invalid action or a Judge rejection uses one round. The tool sends the
  reason to the Fixer, and the repair continues.
- The tool undoes an approved edit that does not change the failure. This
  keeps the final patch small.
- A failed test command of a non-Python repository runs one more time when it
  took less than 15 minutes. The report shows when only the second run passed.
- When the test command runs no tests, the report shows the build result only.
- The Fixer-only mode does not use the Judge.

## Results

Results are saved here:

```text
results/<repo-name>/
```

The folder may contain:

- `report.json`: repair result and metrics
- `fixed_files/`: changed files saved after the application or build passes
- `steps_to_run.txt`: steps saved after the application or build passes
- `agent_logs.json`: optional debugging log

The tool does not edit the original source files. A local Python run creates or
recreates `.venv` inside the selected repository.

### Save agent logs

Use `--agent-logs` when you want to see the repair progress, prompts, and
responses:

```console
uv run repo-repair-agent my-repository --agent-logs
```

The logging option creates one file:

```text
results/my-repository/agent_logs.json
```

The file is updated during the run. If the repair stops early, the completed
log entries are still available. The log may contain repository source code,
so check it before sharing it. Git ignores this file.

## Compare the repair modes

Run the proposed system:

```console
uv run repo-repair-agent my-repository
```

Run the stakeholder Fixer-Judge baseline:

```console
uv run repo-repair-agent my-repository --baseline stakeholder
```

Run the Fixer-only baseline:

```console
uv run repo-repair-agent my-repository --baseline fixer-only
```

The stakeholder baseline uses these models:

- Fixer: `qwen3-coder-30b-a3b-instruct`
- Judge: `qwen3-235b-a22b-instruct-2507`

It comes from
[sakethreddy26/capstone-proposal](https://github.com/sakethreddy26/capstone-proposal),
commit
[2b0cade1c6f7c3445e637ee83e7c247c083f5f3f](https://github.com/sakethreddy26/capstone-proposal/commit/2b0cade1c6f7c3445e637ee83e7c247c083f5f3f).

Each completed run replaces the previous results for that repository. Use
different output folders when you want to keep every evaluation run:

```console
uv run repo-repair-agent my-repository --output results/proposed
uv run repo-repair-agent my-repository --baseline stakeholder --output results/stakeholder
uv run repo-repair-agent my-repository --baseline fixer-only --output results/fixer-only
```

## Useful options

- `--rounds 8`: allow up to eight repair actions. The default is 30.
- `--timeout 300`: allow each repository command to run for 300 seconds. The
  default is 7200 seconds.
- `--live`: use the AI models even when the repository has a `repair.json`.
- `--agent-timeout 1800`: allow 30 minutes in total for Fixer and Judge API
  calls. The default is two hours and the maximum is four hours.
- `--no-install`: do not download repository dependencies.
- `--backend local`: use only the local machine.
- `--backend docker`: use Docker.
- `--docker-image IMAGE`: choose a Docker image.
- `--allow-docker-pull`: allow the selected Docker image to be downloaded or
  built.
- `--script FILE`: use saved offline repair actions instead of the AI models.
- `--config FILE`: use another model configuration file.
- `--output FOLDER`: choose where results are saved.

Run this command to see every option:

```console
uv run repo-repair-agent --help
```

If the selected repository contains `repair.json`, the tool uses it
automatically for an offline run. Use `--live` to ignore that file. The agents
never see `repair.json`. `--script` cannot be used with `--baseline` or
`--live`.

## Python repositories

- A local Python run creates `.venv` inside the selected repository.
- This project's Git settings ignore `.venv` folders under
  `input/broken_repos/`.
- A Docker Python run creates `.venv` only inside the temporary copy.
- Packages in `requirements.txt` are installed automatically unless
  `--no-install` is used.
- In every mode except the stakeholder mode, a package installed by the Fixer
  is added to the repaired `requirements.txt`. It is saved in `fixed_files`
  when the application passes.
- The stakeholder mode does not add installed packages to `requirements.txt`.
- `.python-version` or `requires-python` in `pyproject.toml` can request a
  Python version.
- If that version is not installed, Docker will be used.
- Docker images are downloaded only when `--allow-docker-pull` is used.

## Docker

 Docker will be used for non-Python
repositories and repositories with a Dockerfile. It also uses Docker when a
Python repository requests a Python version that is not installed locally.

Automatic Docker selection:
This command automatically uses Docker when required.
```console
uv run repo-repair-agent my-repository
```

To explicilty used Docker:

```console
uv run repo-repair-agent my-repository --backend docker
```

If the image is missing, the tool stops and shows the image name. After
checking the image, allow the download with:

```console
uv run repo-repair-agent my-repository --backend docker --allow-docker-pull
```

One container runs every command of a repair. It holds only the temporary
copy of the repository. The container has network access, because builds and
tests download dependencies. It uses all host CPUs and three quarters of the
host memory. The tool removes the container when the repair ends.

The container has these folders:

| Folder | Use |
| --- | --- |
| `/workspace/<repo-name>` | The temporary copy of the repository |
| `/home/repair` | Home folder and dependency caches of the build user |
| `/ramdisk` | A memory file system, for tests that depend on the disk type |

Non-Python repositories use the image `repo-repair/sandbox`. The tool builds
it from `docker/sandbox.Dockerfile` on the first run with
`--allow-docker-pull`. The image holds compilers and the supported build
systems only. The agents install each library that a repository needs, so
each install is a reviewed repair step in the report. The image is fresh on
purpose: it has no package lists. The Fixer must run `apt-get update` as a
repair step before it can look up or install a package.

In the proposed mode, the Judge reads the README, the other files in the
repository root, and the build and test lines of the CI files. Then it chooses
the dependency, build, and test commands for Linux. If the Judge gives no
valid answer, the tool uses the default commands of the build system. The
Fixer can propose better commands during the repair.

The tool corrects two kinds of planned commands:

- A build command that downloads or installs a published copy of the project
  (for example `go install module@latest`). The tool uses the default build
  command.
- A dependency command that installs system packages (for example
  `sudo apt install`). Planned commands do not run as root, so the tool does
  not use that command. The agents install system packages as repair steps.

Use another image when a repository needs one:

```console
uv run repo-repair-agent my-repository --backend docker --docker-image IMAGE
```

## Metrics

The console and `report.json` show:

- Correct repair rate
- Fixer tokens
- Judge tokens
- Total tokens
- Total run time

A correct repair means the repository failed first and passed its required
checks after the repair. A repository that already worked is not counted as a
repair.

A code repair without detected tests does not count as correct. An environment
repair can count when an approved package install makes the failing command
pass.

Token counts are estimates made with `tiktoken`. If that cannot be used, the
tool makes a simple estimate with Python.

## Choose the AI models

Model settings are in `config.json`:

```json
{
  "endpoint": "https://openai.rc.asu.edu/v1",
  "fixer_model": "kimi-k2-7-code",
  "judge_model": "qwen3-235b-a22b",
  "api_timeout_seconds": 300
}
```

- `endpoint`: model API address
- `fixer_model`: model that suggests repairs
- `judge_model`: model that reviews code changes
- `api_timeout_seconds`: wait time for one API request

### Requests that the model gateway refuses

The ASU gateway has a firewall. It refuses a request (HTTP 403) when the text
looks like an attack. Build logs, shell scripts, and paths such as `/../` can
cause this. The tool does not change such text. It sends the request again
with less context, in up to five steps:

1. No file snapshot and no CI lines.
2. Only the last exploration result, no evidence files, no root files.
3. No exploration results, and shorter command output.
4. Much shorter command output.
5. A history without free text.

If the gateway refuses all steps, the round fails and the Fixer gets a
message. A repository with much blocked text can thus need more rounds.

## What the tool supports

- Python entry files must be named `main.py`, `app.py`, or `run.py` and must be
  in the repository root.
- Python tests use standard `unittest` discovery.
- Pytest-only projects are not yet supported.
- Supported non-Python builds are Cargo, Go, Meson, SCons, CMake, and Make.

Use only repositories you trust. Repository commands and package installers
run code on your machine or in Docker. During AI repairs, selected source files
and error messages are sent to ASU Voyager. Do not use a repository containing
passwords, API keys, or private data.

## Project folders

- `src/`: repair-agent code
- `input/broken_repos/`: repositories given to the agent
- `results/`: repair results
- `docker/`: the Dockerfile of the build sandbox
- `tests/`: repair-agent tests
- `.env`: local API key
- `config.json`: model settings

## Test the repair-agent code

These tests check the code we wrote to repair repositories. They check the CLI,
repository detection, Fixer and Judge handling, repair safety checks, Docker and
local execution, result files, and metrics. The tests in `tests/` test the
repair-agent code under `src/`.

```console
uv run python -m unittest discover -s tests -v
```
