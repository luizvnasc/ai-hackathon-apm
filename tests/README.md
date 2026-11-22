# Tests

Two layers, both offline with respect to GitLab and Plane:

| Layer | What it proves | Tooling | Network |
|---|---|---|---|
| `unit/` | The bundled scripts: math, loaders, API clients, config handling, CLI | Python 3.8+ stdlib `unittest` | none |
| `evals/` | The skills as an agent sees them: picked for the right requests, ignored for the wrong ones, following their instructions | [promptfoo](https://promptfoo.dev) + Claude Agent SDK | Anthropic API only |

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

Prerequisites: Node.js 18+ and either `ANTHROPIC_API_KEY` or a logged-in local Claude Code (`claude auth status` shows `loggedIn: true`). The configs set `apiKeyRequired: false`, so the local login is used when no key is set; with a key set, the key is used.

```bash
cd apm/tests/evals
npm install                        # first time
./sync-skills.sh                   # fresh copy of ../../.apm/skills into .workspace/.claude/skills (no .env)
PROMPTFOO_DISABLE_TELEMETRY=1 npx promptfoo eval -c <skill-name>/promptfooconfig.yaml --no-share
npx promptfoo view                 # optional: browse the results locally
```

- Evals run against the source skills, never the installed `.claude/skills/` copy. Re-run `./sync-skills.sh` after every skill change.
- Every config loads all skills (`skills: 'all'`), so routing between neighbouring skills is tested too. When you change a `description`, re-run the evals of the neighbouring skills.
- Configs have two providers with the same agent setup: `unconfigured` (blank `GITLAB_*`/`PLANE_*`: routing tests and the `NOT CONFIGURED` path) and `configured` (fake credentials plus the offline mock below). Each test names its provider with `providers: [unconfigured]` or `providers: [configured]`. Never put real tokens in a config and never point one at a real GitLab or Plane.
- `llm-rubric` asserts are graded by a tool-less Claude Agent SDK provider set in `defaultTest.options.provider` (also `apiKeyRequired: false`). Copy that block into new configs.
- Copy `team-velocity/promptfooconfig.yaml` as the template for a new skill.

Cost: every test is a real agent run of a few turns. Both providers use `model: haiku` to keep runs cheap; with the local Claude Code login, runs spend the user's subscription quota. Keep `max_turns` and `max_budget_usd` in the provider config (0.5 USD is the cap per test, a typical test costs far less). Run only the configs you changed, and use `--repeat 3` only for flaky triggers. Add `--no-cache` when you need a fresh run after changing a skill; promptfoo otherwise caches identical requests.

### Offline GitLab and Plane for behaviour evals

The scripts all call `urllib.request.urlopen`. `evals/mock/sitecustomize.py` replaces it, so a skill's script runs unchanged but is answered from JSON fixtures. `sync-skills.sh` copies `mock/` to `.workspace/mock` and `fixtures/` (one dataset shared by all skills) to `.workspace/fixtures/`. The `configured` provider sets:

| Variable | Value |
|---|---|
| `GITLAB_URL`, `GITLAB_TOKEN` | `http://gitlab.mock`, `glpat-eval-fake` |
| `PLANE_URL`, `PLANE_WORKSPACE`, `PLANE_API_KEY` | `http://plane.mock`, `eval`, `plane-eval-fake` |
| `PYTHONPATH` | `{{ env.PWD }}/.workspace/mock` (Python then imports the mock at startup; run promptfoo from `tests/evals`) |
| `EVAL_FIXTURES` | `{{ env.PWD }}/.workspace/fixtures` (the same for every skill) |
| `EVAL_DATASET_DATE` | `2026-11-12`, the date the dataset was written for. The mock shifts every fixture date by (today − this date), so the dataset always ends on the real today: the agent and the script agree on "today" and the numbers never drift. Weekly buckets (Monday–Sunday) still move with the weekday, so eval asserts compare weekly numbers with the script's own output; unit tests pin them with `EVAL_TODAY` (freezes `date.today()`). |

Rules of the mock: hosts are ignored; a request is matched by method, path, query (subset) and, for `POST /api/graphql`, by keywords in the query plus variables (subset). An unmatched request gets HTTP 404 with a JSON body and `[eval-mock] MISS ...` on stderr. **Every write is refused** (HTTP 403, `[eval-mock] WRITE REFUSED ...`): any method but GET, and GraphQL mutations, whatever the manifest says. Write skills can therefore only be evaluated up to their dry run.

Add fixtures for a skill:

1. Add the skill's routes to the shared `evals/fixtures/routes.json` (reuse routes the dataset already has; do not duplicate them), a list of `{"method", "path" | "path_regex", "query", "graphql": {"contains": [...], "variables": {...}}, "file", "status", "headers"}`; first match wins. See `fixtures/routes.json` and the docstring of the mock.
2. Put new response bodies next to it as JSON (trimmed real responses are fine; fake hosts, ids and people, never real data or tokens). Use fake users such as `alice`, project `INV` / `group/inv`, workspace `eval`.
3. Run the script by hand until no `[eval-mock] MISS` is printed:
   `EVAL_FIXTURES=$PWD/tests/evals/fixtures EVAL_TODAY=2026-11-12 EVAL_DATASET_DATE=2026-11-12 PYTHONPATH=$PWD/tests/evals/mock GITLAB_URL=http://gitlab.mock GITLAB_TOKEN=glpat-eval-fake PLANE_URL=http://plane.mock PLANE_WORKSPACE=eval PLANE_API_KEY=plane-eval-fake python3 .apm/skills/<skill>/scripts/<script>.py ...`
4. Design the dataset so the numbers are easy to verify by hand, and add a unit test that runs the script against it in a subprocess (see `unit/eval_mock/test_mock.py`, `VelocityEndToEndTest`). The eval then asserts on those numbers.
5. In the eval config copy the two providers of `team-velocity/promptfooconfig.yaml` and mark each test with its provider. Every test except the deliberate missing-credentials ones runs on `configured` and uses only projects of the dataset (Plane `INV` in workspace `eval`, GitLab `group/inv`). A near miss routed to a skill that has no routes yet asserts routing only and carries a `# TODO fixtures: <skill>` comment.

The mock itself is tested in `unit/eval_mock/`.
