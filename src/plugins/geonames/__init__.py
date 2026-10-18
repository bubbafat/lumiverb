"""Place names from GeoNames (ADR-021, ADR-017 phase 4): a list of the
world's towns, kept on this server, for finding a place by name when
setting a location. Its data is GeoNames', under Creative Commons
Attribution 4.0: accepted when an admin turns it on, credited wherever its
names show."""

from src.plugins.contract import Acknowledgment, Link, PluginSpec

PLUGIN = PluginSpec(
    plugin_id="geonames",
    title="Place names (GeoNames)",
    description=("Find a place by name when setting a location. Lumiverb keeps a list of the world's towns of "
                 "1,000 people or more on this server, from GeoNames."),
    acknowledgment=Acknowledgment(
        version="1",
        title="GeoNames: Creative Commons Attribution 4.0",
        text=(
            "Place names come from GeoNames (geonames.org), which licenses them under the Creative Commons "
            "Attribution 4.0 International license (CC BY 4.0).\n\n"
            "Turning this on downloads the list of towns of 1,000 people or more, with their regions and "
            "countries (about 11 MB), from download.geonames.org, once for this server. Lumiverb keeps a "
            "changed copy: each place's name, point, region, country and population. Nothing about your "
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
    order=100,
)
