from __future__ import annotations

import pytest

from src.upgrade.context import UpgradeContext
from src.upgrade.runner import TenantUpgradeRunner, UpgradeStepNotReadyError
from src.upgrade.step import UpgradeStepInfo


class _FakeMetadataRepo:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get_value(self, key: str) -> str | None:
        return self._data.get(key)

    def set_value(self, key: str, value: str) -> None:
        self._data[key] = value

    def delete_key(self, key: str) -> None:
        self._data.pop(key, None)


class _FakeSession:
    """We don't touch the session in these unit tests."""


class _TestStep:
    def __init__(self, step_id: str, version: str, needs: bool, run_result: dict) -> None:
        self.info = UpgradeStepInfo(step_id=step_id, version=version, display_name=step_id)
        self._needs = needs
        self._run_result = run_result
        self.ran = 0

    def needs_work(self, _ctx: object) -> bool:
        return self._needs

    def run(self, _ctx: object) -> dict:
        self.ran += 1
        return self._run_result


@pytest.mark.fast
def test_upgrade_runner_runs_pending_step_and_marks_completed() -> None:
    meta = _FakeMetadataRepo()
    ctx = UpgradeContext(session=_FakeSession(), metadata=meta, tenant_id="ten_x")

    step1 = _TestStep(step_id="s1", version="v1", needs=True, run_result={"ok": True})
    step2 = _TestStep(step_id="s2", version="v1", needs=False, run_result={"ok": False})

    runner = TenantUpgradeRunner(steps=[step1, step2])
    status_before = runner.get_status(ctx)
    assert status_before["has_work"] is True
    assert status_before["pending_steps"] == 1
    assert status_before["done_steps"] == 1  # s2 skipped

    result = runner.execute(ctx, max_steps=1)
    assert len(result.ran_steps) == 1
    assert result.ran_steps[0]["step_id"] == "s1"
    assert step1.ran == 1
    assert step2.ran == 0

    status_after = runner.get_status(ctx)
    assert status_after["has_work"] is False
    assert status_after["pending_steps"] == 0
    assert status_after["completed_steps"] == 1  # s1 completed
    assert status_after["done_steps"] == 2  # s1 completed + s2 skipped


@pytest.mark.fast
def test_upgrade_runner_is_idempotent_after_completion() -> None:
    meta = _FakeMetadataRepo()
    ctx = UpgradeContext(session=_FakeSession(), metadata=meta, tenant_id="ten_x")

    step1 = _TestStep(step_id="s1", version="v1", needs=True, run_result={"n": 1})
    runner = TenantUpgradeRunner(steps=[step1])

    runner.execute(ctx, max_steps=1)
    assert step1.ran == 1

    # Second execute should do nothing because completed marker matches version.
    status = runner.get_status(ctx)
    assert status["has_work"] is False
    result2 = runner.execute(ctx, max_steps=1)
    assert result2.ran_steps == []
    assert step1.ran == 1


@pytest.mark.fast
def test_upgrade_runner_step_requires_preceding_steps_unless_force() -> None:
    meta = _FakeMetadataRepo()
    ctx = UpgradeContext(session=_FakeSession(), metadata=meta, tenant_id="ten_x")

    step1 = _TestStep(step_id="s1", version="v1", needs=True, run_result={"ok": True})
    step2 = _TestStep(step_id="s2", version="v1", needs=True, run_result={"ok": True})
    runner = TenantUpgradeRunner(steps=[step1, step2])

    # s2 cannot run while s1 is still pending.
    with pytest.raises(UpgradeStepNotReadyError) as ei:
        runner.execute_with_options(ctx, max_steps=1, step_id="s2", force=False)
    assert ei.value.not_done_preceding_step_ids == ["s1"]

    # Safety valve: with force=True, s2 runs.
    result = runner.execute_with_options(ctx, max_steps=1, step_id="s2", force=True)
    assert len(result.ran_steps) == 1
    assert result.ran_steps[0]["step_id"] == "s2"
    assert step2.ran == 1

