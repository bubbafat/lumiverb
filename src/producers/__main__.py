"""`uv run python -m src.producers --write`: docs/data-names.md written again from the producers and
src/producers/names.py (without --write, printed)."""

import sys

from src.producers.names import main

sys.exit(main(sys.argv[1:]))
