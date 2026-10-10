# Phase 0 — Implementation Summary

**Status:** complete · **Commit:** `2147619` "Phase 0: project foundation" · **Date:** 2026-10-10

Phase 0 sets up the foundation only: repo layout, tooling, CI, and project memory. There is no service, agent,
guardrail, or eval logic yet.

## 1. Environment check

| Component | Found | Status |
|---|---|---|
| OS | macOS 26.6.2, Apple Silicon (arm64) | OK. `make` is available, so no `scripts/tasks.py` is needed |
| Python | 3.12.15 (project `.venv`), 3.11.17 also installed; both from `uv` | OK (≥ 3.11 required) |
| uv | installed | OK; `make install` uses it |
| git | 2.50.1 | OK |
| make | GNU Make 3.81 | OK (old version, so the Makefile avoids features from Make 4.x) |
| gh | 2.101.0 | OK |
| docker | CLI 29.8.1 | **Broken, see §6** |

Note: plain `python3` on this machine is 3.14 and `/usr/bin/python3` is 3.9. Always use `.venv/bin/python` or the
`make` targets.

## 2. Repo layout

```
agent/  api/  bank_services/  evals/  monitoring/  scripts/  serving/  tests/   ← Python packages (empty __init__.py)
notebooks/  results/                                                            ← .gitkeep placeholders
docs/                                                                           ← PLAN.md, this summary
.github/workflows/                                                              ← ci.yml
```

## 3. Files created

| File | What it contains |
|---|---|
| `pyproject.toml` | Project metadata (`requires-python >=3.11`). Ruff config: line length 120, rules `E,F,W,I,B,UP`, target py311, first-party packages listed for isort. Pytest config: `testpaths=tests`, `pythonpath=.`, `-ra --strict-markers`. |
| `requirements.txt` | 10 runtime libraries, each pinned to its current major version |
| `requirements-dev.txt` | `-r requirements.txt` + pytest + ruff |
| `.gitignore` | venvs, caches, logs, `results/**/trials.jsonl`, `results/**/llm_requests.jsonl`, `.env*`, notebook checkpoints, OS/editor files |
| `Makefile` | Working targets: `help`, `install`, `lint`, `format`, `test`. Placeholders for phases 1–9. |
| `tests/test_smoke.py` | A parametrized import test for all 7 code packages |
| `.github/workflows/ci.yml` | Runs on push and PR with Python 3.12. Installs `requirements-dev.txt` (pip cached), then runs ruff check, ruff format check, and pytest. Read-only token permissions; a newer run on the same ref cancels the older one. |
| `CLAUDE.md` | Project memory: architecture rules, metrics-integrity rules, repo layout, commands, phase plan, git rules |
| `docs/PLAN.md` | Phases 0–9, each with a definition-of-done checklist, plus a global definition of done |
| `README.md` | One-paragraph description, "Status: phase 0" banner, setup commands |

### Dependency pins and installed versions

Pins are ranges: `>=X.Y,<X+1`. For packages still on 0.x, a minor bump can break things, so those are pinned to the
minor (`>=0.Y,<0.Y+1`). The versions `make install` installed on 2026-10-10 (59 packages in total including
transitive dependencies):

| Package | Pin | Installed |
|---|---|---|
| langgraph | `>=1.2,<2` | 1.2.14 |
| langchain-core | `>=1.6,<2` | 1.6.9 |
| langchain-openai | `>=1.7,<2` | 1.7.0 |
| openai | `>=3.28,<4` | 3.28.0 |
| fastapi | `>=0.143,<0.144` | 0.143.0 |
| uvicorn[standard] | `>=0.54,<0.55` | 0.54.0 |
| httpx | `>=0.28,<0.29` | 0.28.1 |
| pydantic | `>=2.14,<3` | 2.14.0 |
| prometheus-client | `>=0.26,<0.27` | 0.26.0 |
| pyyaml | `>=6.0,<7` | 6.0.3 |
| pytest (dev) | `>=9.1,<10` | 9.1.1 |
| ruff (dev) | `>=0.17,<0.18` | 0.17.0 |

### Make targets

| Target | Phase | Behaviour |
|---|---|---|
| `install` | 0 | Creates `.venv` (Python 3.12) if missing and installs `requirements-dev.txt`. Uses `uv` when present, otherwise `venv` + `pip`. |
| `lint` | 0 | `ruff check .` then `ruff format --check .` |
| `format` | 0 | `ruff check --fix .` then `ruff format .` |
| `test` | 0 | `pytest` |
| `help` | 0 | Lists every target with its description (the default goal) |
| `seed`, `services` | 1 | Prints "not implemented yet (phase 1)" |
| `eval` | 4 | Placeholder |
| `redteam` | 5 | Placeholder |
| `serve-fp16`, `serve-awq`, `bench` | 6 | Placeholder |
| `api`, `monitoring-up` | 7 | Placeholder |
| `docker-build`, `compose-up` | 8 | Placeholder |
| `report` | 9 | Placeholder |

## 4. Design decisions

| Decision | Why |
|---|---|
| Reuse the existing `.venv` (Python 3.12, created by uv) | It matches the CI Python version. That venv has no pip, so `make install` installs with `uv pip`. |
| No build backend in `pyproject.toml` | The requirements files are the single source of dependencies. Packages are imported from the repo root (`pythonpath=.`), so nothing has to be pip-installed. |
| `lint` also runs `ruff format --check` | Keeps formatting consistent from the first commit. CI runs the same check. |
| `scripts/` is a package | Later result-producing scripts can run as `python -m scripts.<name>`. |
| Placeholder targets exit 0 | As requested, they only print a message. Each one must be implemented before any CI gate relies on it (phase 8). |
| Architecture rules written into `CLAUDE.md` | Every later phase reads this file, so the rules stay consistent: guardrails L1–L6, identity from the session, payments and admin operations never on the allow-list, grading on end state, every metric written by a script. |
| No Claude attribution in git | The user asked for this. It is recorded in `CLAUDE.md` and in Claude's project memory. |

## 5. Verification

Everything below was run locally on 2026-10-10.

| Check | Command | Result |
|---|---|---|
| Install | `make install` | All requirements resolved and installed, with no conflicts |
| Lint | `make lint` | `All checks passed!` · `12 files already formatted` |
| Tests | `make test` | `7 passed` (one smoke import per package) |
| Placeholders | `make eval` | `eval: not implemented yet (phase 4)` |
| CI file | `yaml.safe_load(ci.yml)` | Parses; job `lint-and-test` |
| Git | `git log` | One commit, authored by the repo owner, no co-author trailer |

**Not verified yet:** the CI workflow has not run on GitHub, because no remote is configured. Its first real run will
happen on the first push.

## 6. Known issues and follow-ups

1. **Docker is broken locally** (needed from phase 7/8):
   - The active context is `orbstack`, but OrbStack isn't running, so the daemon socket is missing.
   - The `docker-compose` and `docker-buildx` plugins in `~/.docker/cli-plugins/` are symlinks into the OrbStack
     installer disk image (`/Volumes/Install OrbStack v2.2.3/...`), which is no longer mounted.
   - Fix: launch `/Applications/OrbStack.app`, or re-point the symlinks to
     `/Applications/OrbStack.app/Contents/MacOS/xbin/`. Docker Desktop is also installed, so keep only one runtime.
2. **No GitHub remote yet.** Add one and push to trigger the first CI run.
3. **No lockfile.** The ranges allow patch and minor drift between installs. To keep metrics reproducible, later
   phases should record the exact installed versions (e.g. `pip freeze`) in each `results/<run_name>/summary.json`,
   and phase 8 can add a lockfile for container builds.

## 7. Definition of done (from `docs/PLAN.md`)

- [x] Environment checked (Python 3.11+, git, make, docker)
- [x] Repo layout with empty packages (`__init__.py`)
- [x] `pyproject.toml`, `requirements*.txt` with pinned major versions, `.gitignore`
- [x] `Makefile` with working `install`, `lint`, `format`, `test`; placeholder targets for later phases
- [x] `CLAUDE.md` and `docs/PLAN.md`
- [x] Smoke test imports every package; `make lint` and `make test` pass
- [x] GitHub Actions CI (ruff + pytest on push/PR, Python 3.12)
- [x] README stub; `git init` + first commit

**Next:** Phase 1: simulated microservices, deterministic seed data, and a rule-based anomaly detector with
ground-truth tests.
