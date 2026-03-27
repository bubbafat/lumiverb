"""Tests for EXIF parsing helpers in src/workers/exif_extract.py."""

from src.workers.exif_extract import (
    parse_aperture,
    parse_flash_fired,
    parse_focal_length,
    parse_iso,
    parse_lens_model,
    parse_orientation,
    parse_shutter_speed,
)


class TestParseIso:
    def test_integer_value(self):
        assert parse_iso({"ISO": 3200}) == 3200

    def test_string_value(self):
        assert parse_iso({"ISO": "800"}) == 800

    def test_missing(self):
        assert parse_iso({}) is None

    def test_invalid(self):
        assert parse_iso({"ISO": "auto"}) is None


class TestParseShutterSpeed:
    def test_fraction_string(self):
        """String fractions like '1/250' are converted via float to 1/250."""
        assert parse_shutter_speed({"ExposureTime": "1/250"}) == "1/250"

    def test_decimal_fast(self):
        assert parse_shutter_speed({"ExposureTime": "0.004"}) == "1/250"

    def test_decimal_half_second(self):
        assert parse_shutter_speed({"ExposureTime": "0.5"}) == "1/2"

    def test_one_second(self):
        assert parse_shutter_speed({"ExposureTime": "1"}) == "1s"

    def test_long_exposure(self):
        assert parse_shutter_speed({"ExposureTime": "30"}) == "30s"

    def test_numeric_float(self):
        assert parse_shutter_speed({"ExposureTime": 0.004}) == "1/250"

    def test_missing(self):
        assert parse_shutter_speed({}) is None

    def test_empty_string(self):
        assert parse_shutter_speed({"ExposureTime": ""}) is None


class TestParseAperture:
    def test_fnumber(self):
        assert parse_aperture({"FNumber": 2.8}) == 2.8

    def test_aperture_value_fallback(self):
        assert parse_aperture({"ApertureValue": 4.0}) == 4.0

    def test_fnumber_preferred(self):
        assert parse_aperture({"FNumber": 1.4, "ApertureValue": 1.5}) == 1.4

    def test_string_value(self):
        assert parse_aperture({"FNumber": "5.6"}) == 5.6

    def test_missing(self):
        assert parse_aperture({}) is None

    def test_invalid(self):
        assert parse_aperture({"FNumber": "undef"}) is None


class TestParseFocalLength:
    def test_numeric(self):
        assert parse_focal_length({"FocalLength": 35.0}) == 35.0

    def test_string_with_mm(self):
        assert parse_focal_length({"FocalLength": "35 mm"}) == 35.0

    def test_35mm_equivalent(self):
        assert parse_focal_length({"FocalLengthIn35mmFormat": 50}, "FocalLengthIn35mmFormat") == 50.0

    def test_missing(self):
        assert parse_focal_length({}) is None


class TestParseFlashFired:
    def test_fired(self):
        assert parse_flash_fired({"Flash": "Fired"}) is True

    def test_fired_return_detected(self):
        assert parse_flash_fired({"Flash": "Fired, Return detected"}) is True

    def test_did_not_fire(self):
        assert parse_flash_fired({"Flash": "Did not fire"}) is False

    def test_no_flash(self):
        assert parse_flash_fired({"Flash": "No Flash"}) is False

    def test_off(self):
        assert parse_flash_fired({"Flash": "Off, Did not fire"}) is False

    def test_missing(self):
        assert parse_flash_fired({}) is None

    def test_unknown_value(self):
        assert parse_flash_fired({"Flash": "Unknown (0x30)"}) is None


class TestParseLensModel:
    def test_lens_model(self):
        assert parse_lens_model({"LensModel": "RF 24-70mm F2.8L IS USM"}) == "RF 24-70mm F2.8L IS USM"

    def test_lens_id_fallback(self):
        assert parse_lens_model({"LensID": "EF 50mm f/1.4 USM"}) == "EF 50mm f/1.4 USM"

    def test_lens_model_preferred(self):
        assert parse_lens_model({"LensModel": "RF 35mm", "LensID": "RF 35"}) == "RF 35mm"

    def test_missing(self):
        assert parse_lens_model({}) is None


class TestParseOrientation:
    def test_normal(self):
        assert parse_orientation({"Orientation": 1}) == 1

    def test_rotated(self):
        assert parse_orientation({"Orientation": 6}) == 6

    def test_string(self):
        assert parse_orientation({"Orientation": "3"}) == 3

    def test_missing(self):
        assert parse_orientation({}) is None
