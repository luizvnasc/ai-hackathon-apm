# Tests

Two layers:

| Layer | What it proves | Tooling | Network |
|---|---|---|---|
| `unit/` | The bundled scripts: math, loaders, API clients, config handling, CLI | Python 3.8+ stdlib `unittest` | none |
| `evals/` | The skills as an agent sees them: picked for the right requests, ignored for the wrong ones, following their instructions | [promptfoo](https://promptfoo.dev) + Claude Agent SDK | Anthropic Messages API only, through the sandbox proxy |

## Unit tests

```bash
cd apm
python3 -m unittest discover -s tests/unit -t . -v
find . -name __pycache__ -type d -prune -exec rm -rf {} +   # clean up afterwards
```

Layout (one package per skill, `-` replaced by `_`, since packages cannot have hyphens):

```
tests/unit/_load.py                       # load("<skill>", "<script>.py") imports a script by path
tests/unit/<skill_name>/__init__.py
tests/unit/<skill_name>/test_<topic>.py
tests/unit/<skill_name>/fixtures/*.json   # trimmed API responses, no real names, emails or tokens
```

In a test module:

```python
from tests.unit._load import load

vel = load("team-velocity", "velocity.py")
```

Every `load` call executes the script again and returns a fresh module, so patches in one test module never leak into another.

Patterns used in `team_velocity/` that other skills can copy:
- API clients without a network: `Cls.__new__(Cls)` and set the attributes, then patch `req`/`list`/`gql` (or the module's `http`) with `unittest.mock`. Patch `module.time.sleep` when retries are involved.
- CLI: run `module.main([...])` in a temporary working directory (no `.env`, no `.mcp.json`) under `mock.patch.dict(os.environ, {...}, clear=True)`, capture stdout/stderr, catch `SystemExit`. See `Run` in `team_velocity/test_cli.py`.
- Whole reports: replace the `GitLab`/`Plane` class in the module with a fake whose `periods()`/`issues()` return plain dicts, then check `--format json`.
- Secrets: give tokens recognisable fake values (`glpat-secret-123`) and assert they never appear in stdout, stderr or exception messages.

## Skill evals (promptfoo)

The agent under test has an unrestricted Bash, so the evals run **only inside `evals/sandbox`**. They need
Docker (Linux, macOS or WSL) and a credential of their own:

```bash
cd apm/tests/evals
python3 sandbox/run.py --selftest        # containment proof: no credential, no API call

export CLAUDE_CODE_OAUTH_TOKEN=...       # from `claude setup-token`, or export ANTHROPIC_API_KEY
python3 sandbox/run.py team-velocity     # only the skills you changed; results in output/<skill>.json
python3 sandbox/run.py assign-issues --repeat 3 --filter-pattern "direct assign"
```

| | Agent container | Proxy container |
|---|---|---|
| Network | none | the proxy only calls `api.anthropic.com` |
| Host files | none: the skills and evals are streamed in as a tar (no `.env`, no symlinks, nothing git ignores) | none |
| Credential | a placeholder | the real one, received on stdin |
| Privileges | not root, no capabilities, read-only root | the same |

- The proxy forwards only Haiku Messages API requests, without server tools, MCP servers or URL/file sources.
- The local Claude Code login is never read or mounted.
- `promptfoo eval` outside the sandbox fails before starting an agent: the configs' `working_dir` exists only in the
  container. Do not change it to make a host run work. A copy or clone of the repo is not isolation.
- Run `--selftest` after changing anything in `sandbox/` or upgrading the packages.
- Limits: Docker shares the host's kernel. The agent can read the skills, configs and expected answers, and could
  tamper with the results of its own run, so read the outputs and treat them as untrusted.
- Do not add evals that ask for something no skill can do: the agent improvises.

- Evals run against the source skills, never the installed `.claude/skills/` copy. The sandbox copies them again on every run.
- Every config loads all skills (`skills: 'all'`), so routing between neighbouring skills is tested too. When you change a `description`, re-run the evals of the neighbouring skills.
- Configs have two providers with the same agent setup: `unconfigured` (blank `GITLAB_*`/`PLANE_*`: routing tests and the `NOT CONFIGURED` path) and `configured` (fake credentials plus the offline mock below). Each test names its provider with `providers: [unconfigured]` or `providers: [configured]`. Never put real tokens in a config and never point one at a real GitLab or Plane.
- `llm-rubric` asserts are graded by a tool-less Claude Agent SDK provider set in `defaultTest.options.provider` (also `apiKeyRequired: false`). Copy that block into new configs.
- Copy `team-velocity/promptfooconfig.yaml` as the template for a new skill.

Cost: every test is a real agent run of a few turns. Both providers use `model: haiku` to keep runs cheap; with the local Claude Code login, runs spend the user's subscription quota. Keep `max_turns` and `max_budget_usd` in the provider config (0.5 USD is the cap per test, a typical test costs far less). Run only the configs you changed, and use `--repeat 3` only for flaky triggers. The sandbox always runs with `--no-cache`.

### Offline GitLab and Plane for behaviour evals

The scripts all call `urllib.request.urlopen`. `evals/mock/sitecustomize.py` replaces it, so a skill's script runs unchanged but is answered from JSON fixtures. `sync-skills.sh` copies `mock/` to `.workspace/mock` and `fixtures/` (one dataset shared by all skills) to `.workspace/fixtures/`. The `configured` provider sets:

| Variable | Value |
|---|---|
| `GITLAB_URL`, `GITLAB_TOKEN` | `http://gitlab.mock`, `glpat-eval-fake` |
| `PLANE_URL`, `PLANE_WORKSPACE`, `PLANE_API_KEY` | `http://plane.mock`, `eval`, `plane-eval-fake` |
| `PYTHONPATH` | `{{ env.PWD }}/.workspace/mock` (Python then imports the mock at startup) |
| `EVAL_FIXTURES` | `{{ env.PWD }}/.workspace/fixtures` (the same for every skill) |
| `EVAL_DATASET_DATE` | `2026-11-12`, the date the dataset was written for. The mock shifts every fixture date by (today − this date), so the dataset always ends on the real today: the agent and the script agree on "today" and the numbers never drift. Weekly buckets (Monday–Sunday) still move with the weekday, so eval asserts compare weekly numbers with the script's own output; unit tests pin them with `EVAL_TODAY` (freezes `date.today()`). |

`EVAL_PLANE_NO_ESTIMATES=1` makes the mock a Plane CE with the bug [makeplane/plane#9933](https://github.com/makeplane/plane/issues/9933): the documented `/estimates/` routes answer 404 `Page not found.`. The configs of the skills that read points set it.

Rules of the mock: hosts are ignored; a request is matched by method, path, query (subset) and, for `POST /api/graphql`, by keywords in the query plus variables (subset). An unmatched request gets HTTP 404 with a JSON body and `[eval-mock] MISS ...` on stderr. **Every write is refused** (HTTP 403, `[eval-mock] WRITE REFUSED ...`): any method but GET, and GraphQL mutations, whatever the manifest says. Write skills can therefore only be evaluated up to their dry run.

Add fixtures for a skill:

1. Add the skill's routes to the shared `evals/fixtures/routes.json` (reuse routes the dataset already has; do not duplicate them), a list of `{"method", "path" | "path_regex", "query", "graphql": {"contains": [...], "variables": {...}}, "file", "expand", "status", "headers"}`; first match wins. See `fixtures/routes.json` and the docstring of the mock.
2. Put new response bodies next to it as JSON (trimmed real responses are fine; fake hosts, ids and people, never real data or tokens). Use fake users such as `alice`, project `INV` / `group/inv`, workspace `eval`.
3. Run the script by hand until no `[eval-mock] MISS` is printed:
   `EVAL_FIXTURES=$PWD/tests/evals/fixtures EVAL_TODAY=2026-11-12 EVAL_DATASET_DATE=2026-11-12 PYTHONPATH=$PWD/tests/evals/mock GITLAB_URL=http://gitlab.mock GITLAB_TOKEN=glpat-eval-fake PLANE_URL=http://plane.mock PLANE_WORKSPACE=eval PLANE_API_KEY=plane-eval-fake python3 .apm/skills/<skill>/scripts/<script>.py ...`
4. Design the dataset so the numbers are easy to verify by hand, and add a unit test that runs the script against it in a subprocess (see `unit/eval_mock/test_mock.py`, `VelocityEndToEndTest`). The eval then asserts on those numbers.
5. In the eval config copy the two providers of `team-velocity/promptfooconfig.yaml` and mark each test with its provider. Every test except the deliberate missing-credentials ones runs on `configured` and uses only projects of the dataset (Plane `INV` in workspace `eval`, GitLab `group/inv`). A near miss routed to a skill that has no routes yet asserts routing only and carries a `# TODO fixtures: <skill>` comment.

The mock itself is tested in `unit/eval_mock/`.
