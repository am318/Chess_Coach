PY := .venv/bin/python
UV := uv

.PHONY: setup test perft tactics smoke package selfplay lint clean app demo demo-serve

setup:
	$(UV) venv --python 3.12
	$(UV) pip install -e ".[dev,coach]"

test:
	$(PY) -m pytest -q

perft:
	$(PY) -m pytest -q tests/test_perft.py

tactics:
	$(PY) -m pytest -q tests/test_tactics.py

smoke:
	$(PY) -m submission.smoke_test

# Full-length clock rehearsal (slow).
smoke-full:
	SMOKE_CLOCK_MS=120000 SMOKE_MAX_PLIES=120 $(PY) -m submission.smoke_test

package: test
	$(PY) -m submission.build
	$(PY) -m submission.smoke_test

selfplay:
	$(PY) -m tests.selfplay.run --games 100

# Build "Chess Coach.app" - a real bundle with a Dock icon and a working Cmd-Q.
# It wraps this checkout rather than vendoring one, so you only need to rerun it
# if you move the repository.
app:
	$(PY) -m coach.desktop.build_app

coach-serve:
	$(PY) -m coach.cli serve

coach-sync:
	$(PY) -m coach.cli sync

coach-analyze:
	$(PY) -m coach.cli analyze

coach-profile:
	$(PY) -m coach.cli profile

# Synthetic games -> analysed demo database in ./demo (no accounts, no network).
demo:
	$(PY) scripts/demo_data.py

demo-serve:
	COACH_TOML=demo/coach.toml COACH_DB=demo/coach.db $(PY) -m coach.cli serve --no-refresh

lint:
	.venv/bin/ruff check engine submission coach tests scripts

clean:
	rm -rf .venv agent.zip .pytest_cache demo **/__pycache__ "Chess Coach.app"
