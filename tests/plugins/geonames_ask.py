"""Asks the GeoNames plug-in's own list (its places.py) every question of
geonames_lists.py, under whatever python3 runs it, as the plug-in would:

  python3 -I geonames_ask.py PROGRAM_FOLDER LIST   -> its answers, JSON, on stdout
"""

import json
import os
import sys

sys.path[:0] = [sys.argv[1], os.path.dirname(os.path.abspath(__file__))]

import geonames_lists  # noqa: E402
import places  # noqa: E402

with places.Places(sys.argv[2]) as found:
    json.dump(geonames_lists.answers(found, places.label), sys.stdout)
