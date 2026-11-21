import pytest


def test_worker_refuses_changed_file_when_refreshing_signed_source(env,monkeypatch):
    db,ol,worker,client=env
    run=worker.create_run('/public')
    worker.enqueue('/public/a.mp4',ol.objects['/public/a.mp4'],run)
    monkeypatch.setattr('aitags.worker.use_fetch_relay',lambda url:True)
    monkeypatch.setattr(ol,'get_media',lambda *a,**kw:{**ol.objects['/public/a.mp4'],'modified':'changed'},raising=False)
    def compress(*args,**kwargs):
        kwargs['refresh_source']()
        pytest.fail('Changed source was accepted')
    monkeypatch.setattr('aitags.worker.compress_video',compress)
    monkeypatch.setattr('aitags.worker.infer',lambda *a,**kw:pytest.fail('Changed source reached inference'))
    assert worker.work_once()
    assert db.one('SELECT state FROM tasks')['state']=='failed'
    assert '视频已变化' in db.one('SELECT error FROM tasks')['error']
