from local_worker import LocalWorker


def test_local_worker_accepts_only_local_actions():
    worker = LocalWorker()

    assert worker.is_local_action('status') is True
    assert worker.is_local_action('open_app') is True
    assert worker.is_local_action('sync_memory') is False

    result = worker.execute_local_action('status', {'platform': 'darwin'})
    assert result['status'] == 'completed'
    assert result['result']['machine'] == 'mac'
