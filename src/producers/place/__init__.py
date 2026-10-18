"""Place names (ADR-021, the Place names plug-in's producer; ADR-017 phase 4):
each clip with a location gets the name of the nearest town within its
radius ("Near Port of Spain, Trinidad and Tobago"), or that there's none
that near. The location named is the one the clip shows: a person's, else
the file's GPS, else an applied guess. The name is kept with that location
(asset_place): when the location changes, the clip is due again, and its
old name stays until the new one is saved. Only while the Place names
plug-in is on in the account (plugin); turned off, names stay and stop
being made. work.py asks the server for each clip's location and nearest
town (POST /v1/places/nearest) and saves what it found (POST /v1/places/names)."""

from src.producers.contract import ALL, FIND, Pool, ProducerSpec, Setting


def located(coord: str, a: str = "a", loc: str = "l") -> str:
    """SQL: the clip's latitude or longitude (coord "lat" or "lon") as it shows, on assets a with its
    asset_location loc LEFT JOINed: a person's, else the file's, else an applied guess; NULL without one."""
    gps = f"{a}.gps_{coord}"
    return (f"(CASE WHEN {loc}.source = 'person' THEN {loc}.{coord}"
            f" WHEN {a}.gps_lat IS NOT NULL AND {a}.gps_lon IS NOT NULL THEN {gps}"
            f" WHEN {loc}.source = 'time' AND {loc}.status = 'applied' THEN {loc}.{coord} END)")


# Arithmetic over rows the database hands it: no GPU, no storage (location guesses' pool).
CPU = Pool("cpu", slots=2)

PRODUCER = ProducerSpec(
    artifact="place", producer="place", version="1", media=ALL, title="Place names", order=125,
    # A location it shows: a person's, the file's GPS, or an applied guess (never a suggestion).
    applies=("(a.gps_lat IS NOT NULL AND a.gps_lon IS NOT NULL) OR EXISTS (SELECT 1 FROM asset_location pl"
             " WHERE pl.asset_id = a.asset_id AND (pl.source = 'person' OR (pl.source = 'time'"
             " AND pl.status = 'applied')))"),
    # Named from the location it shows now.
    made=("EXISTS (SELECT 1 FROM asset_place ap LEFT JOIN asset_location l ON l.asset_id = ap.asset_id"
          f" WHERE ap.asset_id = a.asset_id AND ap.lat = {located('lat')} AND ap.lon = {located('lon')})"),
    settings=(
        Setting("radius_km", 25, "Nearest town within", minimum=1, maximum=200, unit="km"),
    ),
    # Its own file changing changes nothing here; its location changing does (made).
    redo_on_source_change=False,
    plugin="geonames",
    kind="place", flag="missing_place", run="src.producers.place.work:Name", tier=FIND, pool=CPU,
    batch=200,
)
