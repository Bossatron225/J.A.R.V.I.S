from local_worker import ACTION_HANDLERS, LOCAL_ACTIONS, LocalWorker


def test_every_local_action_has_a_handler_or_is_special_cased():
    # 'status' is handled inline in execute_local_action rather than via ACTION_HANDLERS.
    special_cased = {'status'}
    missing = LOCAL_ACTIONS - special_cased - ACTION_HANDLERS.keys()
    assert not missing, f"Local actions with no ACTION_HANDLERS entry (would fail as 'unavailable' when relayed from the VPS): {missing}"


def test_security_biometrics_and_capture_screen_resolve_to_real_handlers():
    worker = LocalWorker()
    assert callable(worker._load_handler('security_biometrics'))
    assert callable(worker._load_handler('capture_screen'))


def test_local_worker_accepts_only_local_actions():
    worker = LocalWorker()

    assert worker.is_local_action('status') is True
    assert worker.is_local_action('open_app') is True
    assert worker.is_local_action('sync_memory') is False

    result = worker.execute_local_action('status', {'platform': 'darwin'})
    assert result['status'] == 'completed'
    assert result['result']['machine'] == 'mac'


def test_local_worker_executes_real_handler(monkeypatch):
    worker = LocalWorker()
    monkeypatch.setattr(worker, '_load_handler', lambda action: lambda parameters, **kwargs: f"read {parameters['limit']} messages")

    result = worker.execute_local_action('imessage_control', {'action': 'read_latest', 'limit': 3})

    assert result['status'] == 'completed'
    assert result['result'] == 'read 3 messages'
