import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


@pytest.mark.fast
def test_tiff_pyvips_primary_success_no_pillow_fallback() -> None:
    from src.workers import proxy as proxy_mod

    source_path = Path("some.tif")
    sentinel_proxy = MagicMock(name="proxy_img")

    fake_vips_loaded = MagicMock(name="vips_img")
    fake_vips_loaded.thumbnail_image.return_value = sentinel_proxy
    sentinel_proxy.copy_memory.return_value = sentinel_proxy

    with (
        patch.object(
            proxy_mod.pyvips.Image, "new_from_file", return_value=fake_vips_loaded
        ) as new_from_file,
        patch.object(proxy_mod.PILImage, "open") as pil_open,
    ):
        out = proxy_mod._load_tiff_proxy_image(
            source_path,
            width_orig=5000,
            height_orig=4000,
        )

    assert out is sentinel_proxy
    pil_open.assert_not_called()

    assert new_from_file.call_count == 1
    _, kwargs = new_from_file.call_args
    assert kwargs["access"] == proxy_mod.pyvips.enums.Access.SEQUENTIAL
    assert kwargs["fail_on"] == proxy_mod.pyvips.enums.FailOn.NONE


@pytest.mark.fast
def test_tiff_pyvips_failure_falls_back_to_pillow() -> None:
    from src.workers import proxy as proxy_mod

    source_path = Path("some.tif")
    sentinel_proxy = MagicMock(name="proxy_img")

    fake_pil_img = MagicMock(name="pil_img")

    with (
        patch.object(
            proxy_mod.pyvips.Image,
            "new_from_file",
            side_effect=RuntimeError("vips failure"),
        ),
        patch.object(proxy_mod.PILImage, "open", return_value=fake_pil_img) as pil_open,
        patch.object(proxy_mod, "_pil_to_vips", return_value=sentinel_proxy) as pil_to_vips,
    ):
        out = proxy_mod._load_tiff_proxy_image(
            source_path,
            width_orig=4000,
            height_orig=4000,  # 16MP < default TIFF_MAX_PIXELS (25MP)
        )

    assert out is sentinel_proxy
    pil_open.assert_called_once_with(source_path)
    fake_pil_img.thumbnail.assert_called_once_with(
        (proxy_mod.PROXY_LONG_EDGE, proxy_mod.PROXY_LONG_EDGE),
        proxy_mod.PILImage.LANCZOS,
    )
    pil_to_vips.assert_called_once_with(fake_pil_img)


@pytest.mark.fast
def test_tiff_oversize_guard_raises_without_pillow_decode() -> None:
    from src.workers import proxy as proxy_mod

    source_path = Path("some.tif")

    with (
        patch.object(proxy_mod, "TIFF_MAX_PIXELS", 10),
        patch.object(
            proxy_mod.pyvips.Image,
            "new_from_file",
            side_effect=RuntimeError("vips failure"),
        ),
        patch.object(proxy_mod.PILImage, "open") as pil_open,
    ):
        with pytest.raises(RuntimeError, match=r"TIFF too large to proxy safely"):
            proxy_mod._load_tiff_proxy_image(
                source_path,
                width_orig=10,
                height_orig=2,  # pixel_count = 20 > 10
            )

    pil_open.assert_not_called()

