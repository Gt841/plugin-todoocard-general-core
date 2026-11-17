from __future__ import annotations

import asyncio
from types import SimpleNamespace

from todoocard_core import ble


class FakeBackend:
    def __init__(self, *, fail: bool = False, getter_name: str = "_get_services"):
        self.services = {"stale": True}
        self.discoveries = 0
        self._fail = fail
        setattr(self, getter_name, self._get_services)

    async def _get_services(self):
        self.discoveries += 1
        if self._fail:
            raise RuntimeError("discovery failed")
        self.services = {"fresh": True}
        return self.services


class FakeClient:
    def __init__(self, backend):
        self._backend = backend


def run(coro):
    return asyncio.run(coro)


def test_refresh_is_macos_only(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(ble, "_platform", lambda: "windows")
    logs: list[str] = []
    assert run(ble.refresh_gatt_cache(FakeClient(backend), log=logs.append, settle_seconds=0)) is False
    assert backend.discoveries == 0
    assert backend.services == {"stale": True}
    assert logs == []


def test_refresh_rediscovers_after_service_changed(monkeypatch):
    monkeypatch.setattr(ble, "_platform", lambda: "macos")
    backend = FakeBackend()
    logs: list[str] = []
    assert run(ble.refresh_gatt_cache(FakeClient(backend), log=logs.append, settle_seconds=0)) is True
    # 旧缓存必须先失效，再由 bleak 重新发现
    assert backend.discoveries == 1
    assert backend.services == {"fresh": True}
    assert any("Service Changed" in line for line in logs)


def test_refresh_keeps_cache_when_discovery_fails(monkeypatch):
    monkeypatch.setattr(ble, "_platform", lambda: "macos")
    backend = FakeBackend(fail=True)
    logs: list[str] = []
    assert run(ble.refresh_gatt_cache(FakeClient(backend), log=logs.append, settle_seconds=0)) is False
    assert backend.discoveries == 1
    assert backend.services == {"stale": True}
    assert any("keeping cached handles" in line for line in logs)


def test_refresh_without_backend_api_is_noop(monkeypatch):
    monkeypatch.setattr(ble, "_platform", lambda: "macos")
    logs: list[str] = []
    assert run(ble.refresh_gatt_cache(object(), log=logs.append, settle_seconds=0)) is False

    plain_backend = SimpleNamespace(services={"stale": True})
    assert run(ble.refresh_gatt_cache(FakeClient(plain_backend), log=logs.append, settle_seconds=0)) is False
    assert plain_backend.services == {"stale": True}
