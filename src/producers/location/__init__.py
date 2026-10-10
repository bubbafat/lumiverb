"""Location guesses (ADR-017 phase 3): a clip without GPS placed from the
fixes taken around the same time (GPS from the file, or a person's
location), from any device in its library, with each device's clock
corrected per day from scenes it shares with others. The math is infer.py;
work.py asks the server for the fixes and saves what it found
(PUT /v1/assets/locations/guess/{id}). Off until Infer location is on."""

from src.producers.contract import ALL, FIND, Pool, ProducerSpec, Setting

INFER_DEFAULT = False

# Inferring is on (its setting, as the account has it): SQL, so the clips it
# applies to are none while it's off.
INFER_ON = ("COALESCE((SELECT CAST(CAST(sm.value AS jsonb) ->> 'infer_location' AS boolean)"
            f" FROM system_metadata sm WHERE sm.key = 'producer.location'), {str(INFER_DEFAULT).lower()})")

# Arithmetic over rows the database hands it: no GPU, no storage.
CPU = Pool("cpu", slots=2)

PRODUCER = ProducerSpec(
    artifact="location", producer="location", version="1", media=ALL, title="Location guesses", order=120,
    # No location from the file or a person, and a time to go by.
    applies=(f"{INFER_ON} AND a.taken_at IS NOT NULL AND (a.gps_lat IS NULL OR a.gps_lon IS NULL)"
             " AND NOT EXISTS (SELECT 1 FROM asset_location pl WHERE pl.asset_id = a.asset_id"
             " AND pl.source = 'person')"),
    # Tried: a guess, or nothing within the window (outcome empty); and a
    # guess not due to be checked again (a fix near it changed, came or went).
    made=("EXISTS (SELECT 1 FROM artifact_lineage ll WHERE ll.asset_id = a.asset_id"
          " AND ll.artifact = 'location' AND ll.producer = 'location')"
          " AND NOT EXISTS (SELECT 1 FROM asset_location rl WHERE rl.asset_id = a.asset_id"
          " AND rl.recheck IS NOT NULL)"),
    settings=(
        Setting("infer_location", INFER_DEFAULT, "Infer location", kind="bool"),
        # Not in lineage: a new window checks guesses again (regroup), keeping
        # each unless the new one is surer or its fixes are now outside it.
        Setting("inference_minutes", 360, "Inference window", minimum=5, maximum=1440, unit="min",
                remakes=False),
    ),
    # Made from the clip's capture time and zone, read again from the file first.
    needs=("capture",),
    # Its own file changing changes nothing here; its capture time does (which remakes it).
    redo_on_source_change=False,
    on_settings="src.server.repository.locations:settings_changed",
    regroup="src.server.repository.locations:window_changed",
    kind="location", flag="missing_location", run="src.producers.location.work:Locate", tier=FIND, pool=CPU,
    batch=200,
)
