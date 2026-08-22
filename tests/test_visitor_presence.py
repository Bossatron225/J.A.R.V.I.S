import time

import main as main_module


class _DummyUI:
    def __init__(self):
        self.logs = []

    def is_biometric_lock_active(self) -> bool:
        return False

    def write_log(self, *args, **_kwargs) -> None:
        self.logs.append(" ".join(str(a) for a in args))


def _make_live():
    return main_module.JarvisLive(_DummyUI())


def test_known_visitor_arrival_notifies_once_then_suppressed_while_present(monkeypatch) -> None:
    live = _make_live()
    notified = []
    monkeypatch.setattr(main_module, "notify_user", lambda msg, attachment_path=None: notified.append(msg))

    profiles = {"primary": {"name": "James Lumsden"}, "authorized": {}}
    now = 1000.0

    live._handle_known_visitor_seen({"profile_key": "primary"}, profiles, now, cooldown=1800)
    live._handle_known_visitor_seen({"profile_key": "primary"}, profiles, now + 1, cooldown=1800)

    assert notified == ["James Lumsden was just seen at the camera."]
    assert live._present_known["primary"] == now + 1


def test_known_visitor_departure_then_return_notifies_again(monkeypatch) -> None:
    live = _make_live()
    notified = []
    monkeypatch.setattr(main_module, "notify_user", lambda msg, attachment_path=None: notified.append(msg))

    profiles = {"primary": {"name": "James Lumsden"}, "authorized": {}}

    live._handle_known_visitor_seen({"profile_key": "primary"}, profiles, now=1000.0, cooldown=0)
    live._sweep_departed_visitors(now=1000.0 + 100, debounce=8)  # long gone
    assert "primary" not in live._present_known

    live._handle_known_visitor_seen({"profile_key": "primary"}, profiles, now=1000.0 + 100, cooldown=0)

    assert notified == [
        "James Lumsden was just seen at the camera.",
        "James Lumsden was just seen at the camera.",
    ]


def test_unknown_visitor_lingering_does_not_re_record_sighting(monkeypatch) -> None:
    live = _make_live()
    record_calls = []

    def _fake_record(embedding, **kwargs):
        record_calls.append(embedding)
        return {"visitor_id": "visitor-1", "sighting_count_at_time": len(record_calls)}

    monkeypatch.setattr(main_module, "record_unknown_sighting", _fake_record)
    monkeypatch.setattr(main_module, "notify_user", lambda msg, attachment_path=None: None)
    import auth as auth_module
    monkeypatch.setattr(auth_module, "embedding_similarity", lambda a, b: 0.9)
    monkeypatch.setattr(auth_module, "_sface_threshold", lambda: 0.363)

    result = {"embedding": "embedding-a", "score": 0.5, "frame": None}
    live._handle_unknown_visitor_seen(result, camera_index=0, cluster_window_days=30, now=1000.0, cooldown=1800)
    live._handle_unknown_visitor_seen(result, camera_index=0, cluster_window_days=30, now=1001.0, cooldown=1800)

    assert len(record_calls) == 1  # second call matched the still-present entry, no re-record
    assert live._present_unknown[0][2] == 1001.0  # last-seen timestamp refreshed


def test_unknown_visitor_departure_is_logged_not_texted(monkeypatch) -> None:
    live = _make_live()
    notified = []
    monkeypatch.setattr(main_module, "notify_user", lambda msg, attachment_path=None: notified.append(msg))
    monkeypatch.setattr(
        main_module, "record_unknown_sighting",
        lambda embedding, **kwargs: {"visitor_id": "visitor-1", "sighting_count_at_time": 1},
    )
    import auth as auth_module
    monkeypatch.setattr(auth_module, "embedding_similarity", lambda a, b: -1.0)
    monkeypatch.setattr(auth_module, "_sface_threshold", lambda: 0.363)

    result = {"embedding": "embedding-a", "score": 0.5, "frame": None}
    live._handle_unknown_visitor_seen(result, camera_index=0, cluster_window_days=30, now=1000.0, cooldown=1800)
    live._sweep_departed_visitors(now=1000.0 + 100, debounce=8)

    assert live._present_unknown == []
    assert any("left the camera" in log for log in live.ui.logs)
    assert notified == ["An unrecognized visitor was just seen at the camera."]  # only on arrival
