"""The kill switch on the paths the 5-minute upkeep timer takes (the admin
key: every account). One account's paused Upkeep switch skips only that account, and an
account whose pause can't be read is skipped without stopping the others.
A skipped account is named (`paused_tenants`) and logged, so a run skipped
for a pause reads differently from one with nothing to do (Robert, Oct 9).
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.fast

PAUSED, BROKEN, RUNNING = "ten_paused", "ten_broken", "ten_running"


@contextmanager
def _accounts():
    """Three accounts: one paused, one whose pause can't be read, one running."""

    @contextmanager
    def tenant_session(tenant_id):
        yield SimpleNamespace(tenant_id=tenant_id)

    def upkeep_paused(session):
        if session.tenant_id == BROKEN:
            raise RuntimeError("no processing_paused table")
        return session.tenant_id == PAUSED

    @contextmanager
    def control_session():
        yield MagicMock()

    tenants = [SimpleNamespace(tenant_id=t) for t in (PAUSED, BROKEN, RUNNING)]
    with patch("src.server.database.get_control_session", control_session), \
            patch("src.server.database.get_tenant_session", tenant_session), \
            patch("src.server.repository.control_plane.TenantRepository.list_all", return_value=tenants), \
            patch("src.server.repository.lineage.upkeep_paused", upkeep_paused):
        yield


def test_faces_are_named_only_in_the_accounts_not_paused(caplog):
    from src.server.api.routers.upkeep import _propagate_faces_all_tenants

    done: list[str] = []

    def propagate(repo):
        done.append(repo.session.tenant_id)
        return {"assigned": 1, "scanned": 2}

    with _accounts(), patch("src.server.repository.tenant.FaceRepository.__init__",
                            lambda self, session: setattr(self, "session", session)), \
            patch("src.server.repository.tenant.FaceRepository.propagate_assignments", propagate), \
            caplog.at_level("INFO", logger="src.server.api.routers.upkeep"):
        assert _propagate_faces_all_tenants() == {"assigned": 1, "scanned": 2, "paused": True,
                                                  "paused_tenants": [PAUSED]}
    assert done == [RUNNING]
    assert any(PAUSED in r.getMessage() and "paused" in r.getMessage() for r in caplog.records)


def test_the_trash_is_emptied_only_in_the_accounts_not_paused():
    from src.server.api.routers.upkeep import _purge_expired_trash_all_tenants

    done: list[str] = []

    def purge(session, tenant_id):
        done.append(tenant_id)
        return {"clips": 3, "libraries": 0, "projects": 1}

    with _accounts(), patch("src.server.api.routers.trash.purge_expired_trash", purge):
        assert _purge_expired_trash_all_tenants() == {"clips": 3, "libraries": 0, "projects": 1, "paused": True,
                                                      "paused_tenants": [PAUSED]}
    assert done == [RUNNING]


def test_files_are_cleaned_up_only_in_the_accounts_not_paused(tmp_path):
    from src.server.search import cleanup

    for t in (PAUSED, BROKEN, RUNNING):
        (tmp_path / t).mkdir()
    done: list[str] = []

    def for_tenant(data_dir, tenant_id, session, *, dry_run):
        done.append(tenant_id)
        return cleanup.CleanupResult(orphan_files=1)

    with _accounts(), patch("src.server.config.get_settings", return_value=MagicMock(data_dir=str(tmp_path))), \
            patch.object(cleanup, "run_cleanup_for_tenant", for_tenant):
        result = cleanup.run_cleanup_all_tenants(dry_run=False)
        assert done == [RUNNING] and result.orphan_files == 1
        assert result.paused and result.paused_tenants == [PAUSED]
        assert any(BROKEN in e for e in result.errors)
        done.clear()
        dry = cleanup.run_cleanup_all_tenants(dry_run=True)
        assert dry.orphan_files == 3 and not dry.paused  # a dry run deletes nothing: it reports
    assert sorted(done) == [BROKEN, PAUSED, RUNNING]


def test_nothing_paused_says_so():
    from src.server.api.routers.upkeep import _purge_expired_trash_all_tenants

    with _accounts(), patch("src.server.repository.lineage.upkeep_paused", return_value=False), \
            patch("src.server.api.routers.trash.purge_expired_trash", return_value={"clips": 0}):
        assert _purge_expired_trash_all_tenants() == {"clips": 0, "libraries": 0, "projects": 0, "paused": False,
                                                      "paused_tenants": []}
