"""Location guesses (ADR-017 phase 3): a clip without GPS placed from the
fixes taken around the same time (GPS from the file, or a person's
location), from any device in its library, with each device's clock
corrected per day from scenes it shares with others. The math is infer.py;
work.py asks the server for the fixes and saves what it found
(PUT /v1/assets/locations/guess/{id}). Only in the libraries where Infer
location is on (each library's, libraries.infer_location: off by default)."""

from src.producers.contract import ALL, FIND, Pool, ProducerSpec, Setting

# Its library infers location: SQL, so the clips it applies to are none in a library where it's off.
INFER_ON = "EXISTS (SELECT 1 FROM libraries il WHERE il.library_id = a.library_id AND il.infer_location)"

# Arithmetic over rows the database hands it: no GPU, no storage.
CPU = Pool("cpu", slots=2)

PRODUCER = ProducerSpec(
    artifact="location", producer="location", version="1", media=ALL, title="Location guesses", order=120,
    # No location from the file or a person, and a time to go by.
    applies=(f"{INFER_ON} AND a.taken_at IS NOT NULL AND (a.gps_lat IS NULL OR a.gps_lon IS NULL)"
             " AND NOT EXISTS (SELECT 1 FROM asset_location pl WHERE pl.asset_id = a.asset_id"
             " AND pl.source = 'person')"),
    # Tried: a guess, or nothing within the window (outcome empty).
    made=("EXISTS (SELECT 1 FROM artifact_lineage ll WHERE ll.asset_id = a.asset_id"
          " AND ll.artifact = 'location' AND ll.producer = 'location')"),
    # A guess due to be checked again (a fix near it changed, came or went;
    # the window changed): kept meanwhile, and not missing.
    recheck=("EXISTS (SELECT 1 FROM asset_location rl WHERE rl.asset_id = a.asset_id"
             " AND rl.recheck IS NOT NULL)"),
    settings=(
        # Not in lineage: a new window checks guesses again (regroup), keeping
        # each unless the new one is surer or its fixes are now outside it.
        Setting("inference_minutes", 360, "Inference window", minimum=5, maximum=1440, unit="min",
                remakes=False),
    ),
    # Made from the clip's capture time and zone, read again from the file first.
    needs=("capture",),
    # Its own file changing changes nothing here; its capture time does (which remakes it).
    redo_on_source_change=False,
    regroup="src.server.repository.locations:window_changed",
    kind="location", flag="missing_location", run="src.producers.location.work:Locate", tier=FIND, pool=CPU,
    batch=200,
)
