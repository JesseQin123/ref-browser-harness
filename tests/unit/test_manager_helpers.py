import pytest

from browser_harness import context, manager_helpers


def _manager_response(tmp_path):
    return {
        "ok": True,
        "ready": True,
        "state": "ready",
        "browser_id": "br_123",
        "backend": "managed",
        "shared": False,
        "binding": {
            "browser_id": "br_123",
            "bu_name": "bh_123",
            "runtime_dir": str(tmp_path / "r"),
            "tmp_dir": str(tmp_path / "t"),
            "download_dir": str(tmp_path / "downloads"),
            "artifact_dir": str(tmp_path / "artifacts"),
            "cdp_url": "http://127.0.0.1:4567",
            "cdp_ws": None,
        },
    }


def test_browser_new_activates_binding_and_acquires_lock(monkeypatch, tmp_path):
    acquired = []
    old = context.get_active_binding()
    try:
        monkeypatch.setattr(manager_helpers.manager_client, "new_browser", lambda *args, **kwargs: _manager_response(tmp_path))
        monkeypatch.setattr(
            manager_helpers.manager_client,
            "acquire_execution_for_binding",
            lambda binding: acquired.append(binding.browser_id),
        )

        state = manager_helpers.browser_new(backend="managed", reason="test")
        binding = context.get_active_binding()
    finally:
        if old is not None:
            context.activate_binding(old)
        else:
            context.clear_active_binding()

    assert state["browser_id"] == "br_123"
    assert "binding" not in state
    assert binding is not None
    assert binding.bu_name == "bh_123"
    assert acquired == ["br_123"]


def test_browser_switch_does_not_activate_binding_when_lock_fails(monkeypatch, tmp_path):
    old = context.get_active_binding()
    previous = context.BrowserBinding(
        browser_id="br_old",
        bu_name="bh_old",
        runtime_dir=tmp_path / "old-r",
        tmp_dir=tmp_path / "old-t",
        manager_mode=True,
    )
    context.activate_binding(previous)
    try:
        monkeypatch.setattr(manager_helpers.manager_client, "switch_browser", lambda browser_id: _manager_response(tmp_path))
        monkeypatch.setattr(
            manager_helpers.manager_client,
            "acquire_execution_for_binding",
            lambda binding: (_ for _ in ()).throw(
                manager_helpers.manager_client.ManagerError({
                    "state": "busy",
                    "reason": "browser is currently active in another browser-harness process",
                })
            ),
        )

        with pytest.raises(manager_helpers.manager_client.ManagerError, match="currently active"):
            manager_helpers.browser_switch("br_123")
        active = context.get_active_binding()
    finally:
        if old is not None:
            context.activate_binding(old)
        else:
            context.clear_active_binding()

    assert active == previous


def test_browser_close_releases_lock_and_clears_active_binding(monkeypatch, tmp_path):
    released = []
    closed = []
    old = context.get_active_binding()
    context.activate_binding(context.BrowserBinding(
        browser_id="br_123",
        bu_name="bh_123",
        runtime_dir=tmp_path / "r",
        tmp_dir=tmp_path / "t",
        manager_mode=True,
    ))
    try:
        monkeypatch.setattr(manager_helpers.manager_client, "release_active_execution_lock", lambda: released.append(True))
        monkeypatch.setattr(
            manager_helpers.manager_client,
            "close_browser",
            lambda browser_id=None: closed.append(browser_id) or {"ok": True, "state": "closed", "browser_id": "br_123"},
        )

        state = manager_helpers.browser_close()
        active = context.get_active_binding()
    finally:
        if old is not None:
            context.activate_binding(old)
        else:
            context.clear_active_binding()

    assert state == {"state": "closed", "browser_id": "br_123"}
    assert released == [True]
    assert closed == [None]
    assert active is None
