"""Writes tests/plugins/geonames_golden.json again from today's Place names,
while its code exists (src/plugins/geonames/, src/server/repository/places.py):
today's answers to tests/plugins/geonames_lists.py's questions on its files,
which the GeoNames plug-in is held to (tests/test_geonames_program.py).

  uv run python tests/plugins/geonames_golden.py

It runs the test that compares today's answers with the golden file
(tests/test_geonames_parity.py, a Postgres from testcontainers as the
suite's), telling it to write them first. Check the difference before it's
committed: only what today's code answers differently should change.
"""

import os
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.exit(subprocess.call(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly",
         "tests/test_geonames_parity.py::test_todays_answers_are_the_golden_files"],
        cwd=Path(__file__).parents[2], env={**os.environ, "LUMIVERB_WRITE_GEONAMES_GOLDEN": "1"}))
