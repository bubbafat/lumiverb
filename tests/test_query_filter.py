"""Unit tests for the QueryFilter algebra — no database needed.

Covers:
  - Each leaf filter's to_sql(), to_url_value(), from_url_value() round-trip
  - Each leaf filter's to_json() / from_json() round-trip
  - parse_f_params() with mixed filter lists
  - capabilities() returns all registered filters
  - QuerySpec partitioning (search_terms, structured_filters)
  - needs_rating_join / needs_metadata_join propagation
  - GroupFilter tree structure
  - Edge cases: GPS bounding box, path traversal, empty values
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from src.server.models.query_filter import (
    ApertureRange,
    CameraMake,
    CameraModel,
    ColorLabel,
    Combinator,
    DateRange,
    ExposureRange,
    Favorite,
    FocalLengthRange,
    GroupFilter,
    HasColor,
    HasExposure,
    HasFaces,
    HasGps,
    HasRating,
    IsoRange,
    LeafFilter,
    LensModel,
    LibraryScope,
    MediaType,
    NearLocation,
    PathPrefix,
    PersonFilter,
    QuerySpec,
    SearchTerm,
    StarRange,
    TagFilter,
)
from src.server.models.filter_registry import (
    PREFIX_MAP,
    TYPE_MAP,
    capabilities,
    from_json,
    parse_f_params,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sql(leaf: LeafFilter) -> tuple[str, dict]:
    """Helper: call to_sql on a leaf, returning (fragment, params)."""
    params: dict = {}
    counter = [0]
    fragment = leaf.to_sql(params, counter)
    return fragment, params


def _roundtrip_url(leaf: LeafFilter) -> LeafFilter:
    """Roundtrip through URL serialization."""
    url_val = leaf.to_url_value()
    return leaf.__class__.from_url_value(url_val)


def _roundtrip_json(leaf: LeafFilter) -> LeafFilter:
    """Roundtrip through JSON serialization."""
    data = leaf.to_json()
    return leaf.__class__.from_json(data)


# ---------------------------------------------------------------------------
# SearchTerm
# ---------------------------------------------------------------------------

class TestSearchTerm:
    def test_label(self):
        assert SearchTerm(q="Disney").label() == '"Disney"'

    def test_to_sql_is_ilike_fallback(self):
        frag, params = _sql(SearchTerm(q="castle"))
        assert "ILIKE" in frag
        assert "%castle%" in params.values()

    def test_to_quickwit(self):
        assert SearchTerm(q="hello world").to_quickwit() == "hello world"

    def test_url_roundtrip(self):
        f = SearchTerm(q="Disney world")
        f2 = _roundtrip_url(f)
        assert f2.q == "Disney world"

    def test_json_roundtrip(self):
        f = SearchTerm(q="test")
        f2 = _roundtrip_json(f)
        assert f2.q == "test"

    def test_prefix(self):
        assert SearchTerm.prefix() == "query"


# ---------------------------------------------------------------------------
# LibraryScope
# ---------------------------------------------------------------------------

class TestLibraryScope:
    def test_single_library(self):
        frag, params = _sql(LibraryScope(library_ids=("lib1",)))
        assert "ANY" in frag
        assert ["lib1"] in params.values()

    def test_multi_library_url_roundtrip(self):
        f = LibraryScope(library_ids=("a", "b", "c"))
        f2 = _roundtrip_url(f)
        assert f2.library_ids == ("a", "b", "c")

    def test_comma_separated_parsing(self):
        f = LibraryScope.from_url_value("id1,id2,id3")
        assert f.library_ids == ("id1", "id2", "id3")

    def test_label_single(self):
        assert "Library" in LibraryScope(library_ids=("x",)).label()

    def test_label_multiple(self):
        assert "3" in LibraryScope(library_ids=("a", "b", "c")).label()


# ---------------------------------------------------------------------------
# PathPrefix
# ---------------------------------------------------------------------------

class TestPathPrefix:
    def test_sql_matches_exact_and_children(self):
        frag, params = _sql(PathPrefix(path="vacation/italy"))
        assert "rel_path =" in frag
        assert "rel_path LIKE" in frag
        assert "vacation/italy" in params.values()
        assert "vacation/italy/%" in params.values()

    def test_url_roundtrip(self):
        f = PathPrefix(path="photos/2024")
        assert _roundtrip_url(f).path == "photos/2024"

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="\\.\\."):
            PathPrefix.from_url_value("../etc/passwd")

    def test_label(self):
        assert PathPrefix(path="foo").label() == "/foo"


# ---------------------------------------------------------------------------
# MediaType
# ---------------------------------------------------------------------------

class TestMediaType:
    def test_image_only(self):
        frag, params = _sql(MediaType(types=("image",)))
        assert "media_type" in frag
        assert "image" in params.values()

    def test_both_types(self):
        frag, _ = _sql(MediaType(types=("image", "video")))
        assert "OR" in frag

    def test_label_photos(self):
        assert MediaType(types=("image",)).label() == "Photos only"

    def test_label_videos(self):
        assert MediaType(types=("video",)).label() == "Videos only"

    def test_url_roundtrip(self):
        f = MediaType(types=("image", "video"))
        assert _roundtrip_url(f).types == ("image", "video")

    def test_enum_values(self):
        assert MediaType.enum_values() == ["image", "video"]


# ---------------------------------------------------------------------------
# Camera / Lens (string filters)
# ---------------------------------------------------------------------------

class TestStringFilters:
    @pytest.mark.parametrize("cls,col", [
        (CameraMake, "camera_make"),
        (CameraModel, "camera_model"),
        (LensModel, "lens_model"),
    ])
    def test_sql(self, cls, col):
        f = cls(value="Test")
        frag, params = _sql(f)
        assert f"a.{col}" in frag
        assert "Test" in params.values()

    @pytest.mark.parametrize("cls", [CameraMake, CameraModel, LensModel])
    def test_url_roundtrip(self, cls):
        f = cls(value="Sony A7IV")
        assert _roundtrip_url(f).value == "Sony A7IV"

    @pytest.mark.parametrize("cls", [CameraMake, CameraModel, LensModel])
    def test_json_roundtrip(self, cls):
        f = cls(value="Canon")
        assert _roundtrip_json(f).value == "Canon"

    def test_faceted(self):
        assert CameraMake.faceted() is True
        assert CameraModel.faceted() is True
        assert LensModel.faceted() is True


# ---------------------------------------------------------------------------
# Range filters (int and float)
# ---------------------------------------------------------------------------

class TestIsoRange:
    def test_both_bounds(self):
        frag, params = _sql(IsoRange(min_val=100, max_val=3200))
        assert "iso >=" in frag
        assert "iso <=" in frag

    def test_min_only(self):
        frag, _ = _sql(IsoRange(min_val=400))
        assert ">=" in frag
        assert "<=" not in frag

    def test_max_only(self):
        frag, _ = _sql(IsoRange(max_val=1600))
        assert "<=" in frag
        assert ">=" not in frag

    def test_url_both(self):
        f = IsoRange(min_val=100, max_val=3200)
        assert f.to_url_value() == "100-3200"
        assert _roundtrip_url(f) == f

    def test_url_min_plus(self):
        f = IsoRange(min_val=400)
        assert f.to_url_value() == "400+"
        assert _roundtrip_url(f) == f

    def test_url_max_only(self):
        f = IsoRange(max_val=1600)
        assert f.to_url_value() == "-1600"
        assert _roundtrip_url(f) == f

    def test_url_exact(self):
        f = IsoRange(min_val=800, max_val=800)
        assert f.to_url_value() == "800"
        assert _roundtrip_url(f) == f

    def test_label_range(self):
        assert "100" in IsoRange(min_val=100, max_val=3200).label()
        assert "3200" in IsoRange(min_val=100, max_val=3200).label()

    def test_label_exact(self):
        assert IsoRange(min_val=800, max_val=800).label() == "ISO 800"


class TestApertureRange:
    def test_url_roundtrip(self):
        f = ApertureRange(min_val=1.4, max_val=2.8)
        assert _roundtrip_url(f) == f

    def test_label(self):
        assert "f/" in ApertureRange(min_val=1.4, max_val=2.8).label()

    def test_sql(self):
        frag, _ = _sql(ApertureRange(min_val=1.4, max_val=5.6))
        assert "aperture >=" in frag
        assert "aperture <=" in frag


class TestFocalLengthRange:
    def test_url_roundtrip(self):
        f = FocalLengthRange(min_val=24.0, max_val=70.0)
        assert _roundtrip_url(f) == f

    def test_label(self):
        assert "mm" in FocalLengthRange(min_val=24.0, max_val=70.0).label()


class TestExposureRange:
    def test_url_roundtrip(self):
        f = ExposureRange(min_us=1000, max_us=500000)
        assert _roundtrip_url(f) == f

    def test_sql(self):
        frag, _ = _sql(ExposureRange(min_us=1000))
        assert "exposure_time_us" in frag


# ---------------------------------------------------------------------------
# Boolean filters
# ---------------------------------------------------------------------------

class TestHasExposure:
    def test_true_checks_three_columns(self):
        frag, _ = _sql(HasExposure(value=True))
        assert "iso IS NOT NULL" in frag
        assert "exposure_time_us IS NOT NULL" in frag
        assert "aperture IS NOT NULL" in frag

    def test_false_checks_all_null(self):
        frag, _ = _sql(HasExposure(value=False))
        assert "iso IS NULL" in frag
        assert "exposure_time_us IS NULL" in frag
        assert "aperture IS NULL" in frag

    def test_url_roundtrip(self):
        assert _roundtrip_url(HasExposure(value=True)).value is True
        assert _roundtrip_url(HasExposure(value=False)).value is False


class TestHasGps:
    def test_true(self):
        frag, _ = _sql(HasGps(value=True))
        assert "gps_lat IS NOT NULL" in frag
        assert "gps_lon IS NOT NULL" in frag

    def test_false(self):
        frag, _ = _sql(HasGps(value=False))
        assert "gps_lat IS NULL" in frag


class TestHasFaces:
    def test_true(self):
        frag, _ = _sql(HasFaces(value=True))
        assert "face_count > 0" in frag

    def test_false(self):
        frag, _ = _sql(HasFaces(value=False))
        assert "face_count" in frag
        assert "= 0" in frag


# ---------------------------------------------------------------------------
# NearLocation — GPS bounding box
# ---------------------------------------------------------------------------

class TestNearLocation:
    def test_sql_produces_bounding_box(self):
        f = NearLocation(lat=48.8566, lon=2.3522, radius_km=5.0)
        frag, params = _sql(f)
        assert "gps_lat BETWEEN" in frag
        assert "gps_lon BETWEEN" in frag
        assert len(params) == 4

    def test_url_roundtrip(self):
        f = NearLocation(lat=48.8566, lon=2.3522, radius_km=5.0)
        f2 = _roundtrip_url(f)
        assert abs(f2.lat - 48.8566) < 0.001
        assert abs(f2.lon - 2.3522) < 0.001
        assert f2.radius_km == 5.0

    def test_default_radius(self):
        f = NearLocation.from_url_value("48.85,2.35")
        assert f.radius_km == 1.0

    def test_equator(self):
        """At equator, lat and lon deltas should be roughly equal."""
        f = NearLocation(lat=0.0, lon=0.0, radius_km=10.0)
        _, params = _sql(f)
        vals = list(params.values())
        lat_delta = abs(vals[1] - vals[0])
        lon_delta = abs(vals[3] - vals[2])
        assert abs(lat_delta - lon_delta) < 0.01  # cos(0) = 1

    def test_high_latitude(self):
        """At high latitude, lon delta should be larger than lat delta."""
        f = NearLocation(lat=70.0, lon=25.0, radius_km=10.0)
        _, params = _sql(f)
        vals = list(params.values())
        lat_delta = abs(vals[1] - vals[0])
        lon_delta = abs(vals[3] - vals[2])
        assert lon_delta > lat_delta

    def test_near_pole_clamped(self):
        """Near the pole, latitude is clamped to avoid division by zero."""
        f = NearLocation(lat=90.0, lon=0.0, radius_km=1.0)
        frag, params = _sql(f)
        # Should not crash — clamped to 89.9
        assert len(params) == 4

    def test_label(self):
        assert "5" in NearLocation(lat=0, lon=0, radius_km=5).label()


# ---------------------------------------------------------------------------
# DateRange
# ---------------------------------------------------------------------------

class TestDateRange:
    def test_both_bounds(self):
        f = DateRange(
            from_dt=datetime(2024, 1, 1, tzinfo=timezone.utc),
            to_dt=datetime(2024, 12, 31, tzinfo=timezone.utc),
        )
        frag, params = _sql(f)
        assert "COALESCE" in frag
        assert ">=" in frag
        assert "<" in frag

    def test_to_dt_is_exclusive_next_day(self):
        """to_dt adds one day for exclusive upper bound."""
        f = DateRange(to_dt=datetime(2024, 6, 15, tzinfo=timezone.utc))
        _, params = _sql(f)
        date_val = list(params.values())[0]
        assert date_val.day == 16

    def test_url_roundtrip_both(self):
        f = DateRange(
            from_dt=datetime(2024, 1, 1, tzinfo=timezone.utc),
            to_dt=datetime(2024, 12, 31, tzinfo=timezone.utc),
        )
        f2 = _roundtrip_url(f)
        assert f2.from_dt.year == 2024  # type: ignore
        assert f2.to_dt.month == 12  # type: ignore

    def test_url_from_only(self):
        f = DateRange(from_dt=datetime(2024, 3, 1, tzinfo=timezone.utc))
        assert f.to_url_value() == "2024-03-01,"
        f2 = _roundtrip_url(f)
        assert f2.from_dt is not None
        assert f2.to_dt is None

    def test_url_to_only(self):
        f = DateRange(to_dt=datetime(2024, 6, 15, tzinfo=timezone.utc))
        assert f.to_url_value() == ",2024-06-15"
        f2 = _roundtrip_url(f)
        assert f2.from_dt is None
        assert f2.to_dt is not None


# ---------------------------------------------------------------------------
# Rating filters
# ---------------------------------------------------------------------------

class TestFavorite:
    def test_sql_true(self):
        frag, _ = _sql(Favorite(value=True))
        assert "r.favorite = TRUE" in frag

    def test_sql_false(self):
        frag, _ = _sql(Favorite(value=False))
        assert "r.favorite IS NULL OR r.favorite = FALSE" in frag

    def test_needs_rating_join(self):
        assert Favorite(value=True).needs_rating_join is True

    def test_url_roundtrip(self):
        assert _roundtrip_url(Favorite(value=True)).value is True
        assert _roundtrip_url(Favorite(value=False)).value is False


class TestStarRange:
    def test_sql(self):
        frag, _ = _sql(StarRange(min_val=3, max_val=5))
        assert "COALESCE(r.stars, 0)" in frag

    def test_needs_rating_join(self):
        assert StarRange(min_val=3).needs_rating_join is True

    def test_url_roundtrip(self):
        f = StarRange(min_val=3, max_val=5)
        assert _roundtrip_url(f) == f

    def test_label_exact(self):
        assert "1 star" in StarRange(min_val=1, max_val=1).label()
        assert "3 stars" in StarRange(min_val=3, max_val=3).label()


class TestColorLabel:
    def test_sql_single(self):
        frag, params = _sql(ColorLabel(colors=("red",)))
        assert "r.color IN" in frag
        assert "red" in params.values()

    def test_sql_multi(self):
        frag, params = _sql(ColorLabel(colors=("red", "blue")))
        assert "r.color IN" in frag
        assert len(params) == 2

    def test_needs_rating_join(self):
        assert ColorLabel(colors=("red",)).needs_rating_join is True

    def test_url_roundtrip(self):
        f = ColorLabel(colors=("red", "blue"))
        assert _roundtrip_url(f).colors == ("red", "blue")


class TestHasRating:
    def test_sql_true(self):
        frag, _ = _sql(HasRating(value=True))
        assert "r.user_id IS NOT NULL" in frag

    def test_sql_false(self):
        frag, _ = _sql(HasRating(value=False))
        assert "r.user_id IS NULL" in frag


class TestHasColor:
    def test_sql_true(self):
        frag, _ = _sql(HasColor(value=True))
        assert "r.color IS NOT NULL" in frag


# ---------------------------------------------------------------------------
# PersonFilter
# ---------------------------------------------------------------------------

class TestPersonFilter:
    def test_sql_uses_subquery(self):
        frag, params = _sql(PersonFilter(person_id="p123"))
        assert "SELECT asset_id FROM faces" in frag
        assert "p123" in params.values()

    def test_url_roundtrip(self):
        f = PersonFilter(person_id="p123")
        assert _roundtrip_url(f).person_id == "p123"


# ---------------------------------------------------------------------------
# TagFilter
# ---------------------------------------------------------------------------

class TestTagFilter:
    def test_sql(self):
        frag, params = _sql(TagFilter(value="sunset"))
        assert "m.tags @> jsonb_build_array" in frag, f"TagFilter must query m.tags (LATERAL alias), got: {frag}"
        assert "sunset" in params.values()

    def test_needs_metadata_join(self):
        assert TagFilter(value="sunset").needs_metadata_join is True

    def test_label(self):
        assert TagFilter(value="sunset").label() == "#sunset"


# ---------------------------------------------------------------------------
# Param counter uniqueness
# ---------------------------------------------------------------------------

class TestParamCollision:
    def test_two_camera_makes_get_unique_params(self):
        """Two CameraMake filters produce different bind param names."""
        params: dict = {}
        counter = [0]
        f1 = CameraMake(value="Canon")
        f2 = CameraMake(value="Sony")
        frag1 = f1.to_sql(params, counter)
        frag2 = f2.to_sql(params, counter)
        assert len(params) == 2
        assert "Canon" in params.values()
        assert "Sony" in params.values()
        # Param names are different
        keys = list(params.keys())
        assert keys[0] != keys[1]

    def test_counter_increments(self):
        counter = [0]
        params: dict = {}
        IsoRange(min_val=100, max_val=3200).to_sql(params, counter)
        assert counter[0] == 2  # min and max each increment


# ---------------------------------------------------------------------------
# GroupFilter
# ---------------------------------------------------------------------------

class TestGroupFilter:
    def test_and_group_json(self):
        group = GroupFilter(
            combinator=Combinator.AND,
            children=(CameraMake(value="Canon"), Favorite(value=True)),
        )
        data = group.to_json()
        assert data["op"] == "and"
        assert len(data["children"]) == 2
        assert data["children"][0]["type"] == "camera_make"

    def test_nested_groups(self):
        inner = GroupFilter(
            combinator=Combinator.OR,
            children=(CameraMake(value="Canon"), CameraMake(value="Sony")),
        )
        outer = GroupFilter(
            combinator=Combinator.AND,
            children=(inner, Favorite(value=True)),
        )
        data = outer.to_json()
        assert data["op"] == "and"
        assert data["children"][0]["op"] == "or"


# ---------------------------------------------------------------------------
# QuerySpec
# ---------------------------------------------------------------------------

class TestQuerySpec:
    def test_empty(self):
        spec = QuerySpec()
        assert spec.leaves == []
        assert spec.search_terms == []
        assert spec.structured_filters == []

    def test_partitioning(self):
        spec = QuerySpec(root=GroupFilter(children=(
            SearchTerm(q="Disney"),
            CameraMake(value="Canon"),
            Favorite(value=True),
        )))
        assert len(spec.search_terms) == 1
        assert len(spec.structured_filters) == 2

    def test_needs_rating_join(self):
        spec = QuerySpec(root=GroupFilter(children=(
            CameraMake(value="Canon"),
            Favorite(value=True),
        )))
        assert spec.needs_rating_join is True

    def test_no_rating_join_needed(self):
        spec = QuerySpec(root=GroupFilter(children=(
            CameraMake(value="Canon"),
        )))
        assert spec.needs_rating_join is False

    def test_needs_metadata_join(self):
        spec = QuerySpec(root=GroupFilter(children=(
            TagFilter(value="sunset"),
        )))
        assert spec.needs_metadata_join is True

    def test_to_json(self):
        spec = QuerySpec(root=GroupFilter(children=(
            SearchTerm(q="Disney"),
            CameraMake(value="Canon"),
        )))
        data = spec.to_json()
        assert "filters" in data
        assert len(data["filters"]) == 2
        assert data["filters"][0]["type"] == "query"
        # sort defaults omitted
        assert "sort" not in data

    def test_to_json_custom_sort(self):
        spec = QuerySpec(
            root=GroupFilter(children=(CameraMake(value="Canon"),)),
            sort="file_size",
            direction="asc",
        )
        data = spec.to_json()
        assert data["sort"] == "file_size"
        assert data["direction"] == "asc"

    def test_nested_leaves(self):
        """Leaves extracts from nested groups."""
        inner = GroupFilter(children=(CameraMake(value="Canon"), CameraMake(value="Sony")))
        outer = GroupFilter(children=(inner, Favorite(value=True)))
        spec = QuerySpec(root=outer)
        assert len(spec.leaves) == 3


# ---------------------------------------------------------------------------
# Registry: parse_f_params
# ---------------------------------------------------------------------------

class TestParseFParams:
    def test_mixed_filters(self):
        spec = parse_f_params([
            "query:Disney",
            "camera_make:Canon",
            "favorite:yes",
            "iso:100-3200",
        ])
        assert len(spec.leaves) == 4
        assert len(spec.search_terms) == 1
        assert spec.search_terms[0].q == "Disney"

    def test_unknown_prefix_ignored(self):
        spec = parse_f_params(["unknown:foo", "camera_make:Canon"])
        assert len(spec.leaves) == 1

    def test_bare_text_becomes_search_term(self):
        spec = parse_f_params(["Disney world"])
        assert len(spec.search_terms) == 1
        assert spec.search_terms[0].q == "Disney world"

    def test_sort_and_direction(self):
        spec = parse_f_params([], sort="file_size", direction="asc")
        assert spec.sort == "file_size"
        assert spec.direction == "asc"

    def test_empty(self):
        spec = parse_f_params([])
        assert spec.leaves == []

    def test_invalid_value_skipped(self):
        spec = parse_f_params(["near:invalid", "camera_make:Canon"])
        assert len(spec.leaves) == 1  # near:invalid should fail parsing

    def test_path_traversal_rejected(self):
        spec = parse_f_params(["path:../etc/passwd"])
        assert len(spec.leaves) == 0


# ---------------------------------------------------------------------------
# Registry: from_json
# ---------------------------------------------------------------------------

class TestFromJson:
    def test_basic(self):
        data = {
            "filters": [
                {"type": "query", "value": "Disney"},
                {"type": "camera_make", "value": "Canon"},
                {"type": "favorite", "value": "yes"},
            ],
        }
        spec = from_json(data)
        assert len(spec.leaves) == 3
        assert spec.search_terms[0].q == "Disney"

    def test_with_sort(self):
        data = {
            "filters": [{"type": "camera_make", "value": "Canon"}],
            "sort": "file_size",
            "direction": "asc",
        }
        spec = from_json(data)
        assert spec.sort == "file_size"
        assert spec.direction == "asc"

    def test_unknown_type_ignored(self):
        data = {
            "filters": [
                {"type": "unknown_filter", "value": "foo"},
                {"type": "camera_make", "value": "Canon"},
            ],
        }
        spec = from_json(data)
        assert len(spec.leaves) == 1

    def test_empty(self):
        spec = from_json({})
        assert spec.leaves == []

    def test_filter_entry_is_string_skips_safely(self):
        """Defensive: pre-V2 saved queries occasionally had string entries
        in `filters`. Don't crash — skip the bad entry, keep the good ones,
        return a valid QuerySpec. Regression test for the production crash
        loading collections list with a corrupt smart query in the DB."""
        data = {
            "filters": [
                "camera_make:Canon",  # bad: string instead of dict
                {"type": "favorite", "value": "yes"},
            ],
        }
        spec = from_json(data)
        # The string is skipped; the favorite filter survives.
        assert len(spec.leaves) == 1

    def test_filters_field_is_string_returns_empty(self):
        """Defensive: if `filters` is a string instead of a list, treat
        it as empty rather than iterating its characters and crashing."""
        data = {"filters": "camera_make:Canon"}
        spec = from_json(data)
        assert spec.leaves == []

    def test_data_is_not_dict_returns_empty(self):
        """Defensive: if the saved_query column is somehow a string at
        the top level, return an empty QuerySpec rather than crashing."""
        spec = from_json("not a dict")  # type: ignore[arg-type]
        assert spec.leaves == []


# ---------------------------------------------------------------------------
# Registry: capabilities
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_returns_all_filters(self):
        caps = capabilities()
        prefixes = {c["prefix"] for c in caps}
        assert "query" in prefixes
        assert "camera_make" in prefixes
        assert "iso" in prefixes
        assert "favorite" in prefixes
        assert "tag" in prefixes
        assert "near" in prefixes
        assert "date" in prefixes

    def test_each_has_required_fields(self):
        for cap in capabilities():
            assert "prefix" in cap
            assert "label" in cap
            assert "value_kind" in cap

    def test_faceted_filters_marked(self):
        caps = capabilities()
        camera = next(c for c in caps if c["prefix"] == "camera_make")
        assert camera.get("faceted") is True

    def test_enum_values_present(self):
        caps = capabilities()
        media = next(c for c in caps if c["prefix"] == "media")
        assert "enum_values" in media
        assert "image" in media["enum_values"]

    def test_prefix_map_matches_type_map(self):
        """Every registered filter is in both maps."""
        assert set(PREFIX_MAP.keys()) == {cls.prefix() for cls in PREFIX_MAP.values()}
        assert set(TYPE_MAP.keys()) == {cls.type_name() for cls in TYPE_MAP.values()}
        assert len(PREFIX_MAP) == len(TYPE_MAP)
