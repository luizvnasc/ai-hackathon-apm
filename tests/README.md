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
- Credentials are blank in every config (`GITLAB_*`, `PLANE_*` set to `''`), so scripts stop with `NOT CONFIGURED`. Never put real tokens in a config and never point one at a real GitLab or Plane.
- `llm-rubric` asserts are graded by a tool-less Claude Agent SDK provider set in `defaultTest.options.provider` (also `apiKeyRequired: false`). Copy that block into new configs.
- Copy `team-velocity/promptfooconfig.yaml` as the template for a new skill.

Cost: every test is a real agent run of a few turns. Both providers use `model: haiku` to keep runs cheap; with the local Claude Code login, runs spend the user's subscription quota. Keep `max_turns` and `max_budget_usd` in the provider config (0.5 USD is the cap per test, a typical test costs far less). Run only the configs you changed, and use `--repeat 3` only for flaky triggers. Add `--no-cache` when you need a fresh run after changing a skill; promptfoo otherwise caches identical requests.
