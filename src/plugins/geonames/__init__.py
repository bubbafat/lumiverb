"""Place names from GeoNames (ADR-021, ADR-017 phase 4): a list of the
world's towns and landmarks, kept on this server, for finding a place by
name when setting a location and naming where each clip was. Its data is
GeoNames', under Creative Commons Attribution 4.0: accepted when an admin
turns it on, credited wherever its names show.

Versions 2 (landmarks, a bigger download) and 3 (names in other
languages) of its text are under the same license, so an acceptance of
version 1 stands (reaccept_from, Robert 2026-10-18: Creative Commons to
Creative Commons asks nothing)."""

from src.plugins.contract import Acknowledgment, Link, PluginSpec

PLUGIN = PluginSpec(
    plugin_id="geonames",
    title="Place names (GeoNames)",
    description=("Find a place by name when setting a location, and see the nearest town and the landmark each "
                 "photo or video was taken at. Lumiverb keeps a list of the world's towns of 1,000 people or more "
                 "and its landmarks on this server, from GeoNames."),
    acknowledgment=Acknowledgment(
        version="3",
        reaccept_from="1",
        title="GeoNames: Creative Commons Attribution 4.0",
        text=(
            "Place names come from GeoNames (geonames.org), which licenses them under the Creative Commons "
            "Attribution 4.0 International license (CC BY 4.0).\n\n"
            "Turning this on downloads, once for this server, GeoNames' list of towns of 1,000 people or more "
            "with their regions and countries, its list of every place and which of them have a Wikipedia "
            "article (about 600 MB in all, deleted once read), from download.geonames.org. "
            "Lumiverb keeps a changed copy: the towns, and landmarks such as parks, museums, monuments and "
            "mountains, each place's name and its names in other languages, point, kind, region, country "
            "and population. Nothing about your "
            "photos, videos or account is sent to GeoNames.\n\n"
            "The license lets the data be used, commercially too, as long as GeoNames is credited with a link, "
            "the license is linked and changes are noted. Lumiverb shows that credit wherever place names "
            "appear.\n\n"
            "GeoNames provides the data as is, without warranty of its accuracy, timeliness or completeness."
        ),
        links=(
            Link("GeoNames", "https://www.geonames.org/"),
            Link("The license (CC BY 4.0)", "https://creativecommons.org/licenses/by/4.0/"),
            Link("GeoNames' terms", "https://www.geonames.org/export/"),
        ),
    ),
    data="src.plugins.geonames.data:fetch",
    data_title="The place list",
    data_unit="places",
    # 2: the landmarks beside the towns. 3: their names in other languages.
    data_version=3,
    order=100,
)
