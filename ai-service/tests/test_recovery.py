import json
import httpx
import pytest
from fastapi import HTTPException
from aitags.recovery import Recovery, KEY
from aitags.openlist import OpenList
from aitags.media import gateway, MediaError, media_headers, run_process
from aitags.model import model_post


def failed(env, paths):
    db, ol, worker, _ = env
    run = worker.create_run('/')
    for path in paths:
        obj = {**ol.objects['/public/a.mp4'], 'size': 2048}
        ol.objects[path] = obj
        worker.enqueue(path, obj, run)
    db.execute("UPDATE tasks SET state='failed',error='transient'")
    db.execute("UPDATE videos SET status='failed',error='transient'")
    db.execute("UPDATE directories SET state='failed',page=3")
    db.execute("UPDATE runs SET state='failed'")
    return run


def test_batches_survive_restart_and_keep_successful_files(env, monkeypatch):
    db, ol, worker, _ = env
    run = failed(env, ['/public/a.mp4', '/public/b.mp4', '/public/c.mp4'])
    db.execute("UPDATE videos SET status='done',result=? WHERE path='/public/c.mp4'", (json.dumps({'summary':'keep','tags':['manual']}),))
    before = db.one("SELECT * FROM videos WHERE path='/public/c.mp4'")
    recovery = Recovery(db)
    assert recovery.start(1, 1, rounds=1, interval=0)['total_tasks'] == 2
    assert KEY not in db.settings()
    assert recovery.tick()
    assert db.one("SELECT COUNT(*) AS n FROM tasks WHERE state='queued'")['n'] == 1
    assert db.one('SELECT page FROM directories')['page'] == 3
    assert not Recovery(db).tick()  # active batch must drain
    monkeypatch.setattr('aitags.worker.compress_video', lambda *a, **k: {})
    monkeypatch.setattr('aitags.worker.infer', lambda *a, **k: {'summary': 'real result', 'tags': ['blue']})
    assert worker.work_once()
    db.execute("UPDATE directories SET state='done'")
    worker.finish_runs()
    assert Recovery(db).tick()
    assert worker.work_once()
    worker.finish_runs()
    assert not Recovery(db).tick()
    assert Recovery(db).status()['state'] == 'done'
    assert db.one("SELECT * FROM videos WHERE path='/public/c.mp4'") == before


def test_plan_pause_cancelled_run_and_interpass_cooldown(env):
    db, ol, worker, _ = env
    run = failed(env, ['/public/a.mp4'])
    recovery = Recovery(db)
    recovery.start(1, 0, rounds=2, interval=0)
    recovery.control('pause')
    assert not Recovery(db).tick()
    recovery.control('resume')
    assert Recovery(db).tick()
    db.execute("UPDATE tasks SET state='failed'")
    db.execute("UPDATE videos SET status='failed'")
    db.execute("UPDATE runs SET state='cancelled'")
    # Do not revive a user-cancelled scan on the second pass.
    db.execute("UPDATE directories SET state='done'")
    assert not Recovery(db).tick()
    plan = Recovery(db).status()
    assert plan['round'] == 2 and plan['next_batch_at'] > plan['created']
    plan['next_batch_at'] = 0
    db.execute('UPDATE settings SET value=? WHERE `key`=?', (json.dumps(plan), KEY))
    assert not Recovery(db).tick()
    assert db.one('SELECT state FROM runs')['state'] == 'cancelled'
    assert Recovery(db).status()['state'] == 'done'


def test_manual_retry_never_requeues_historical_success(env):
    db, ol, worker, _ = env
    run = failed(env, ['/public/a.mp4'])
    db.execute("UPDATE videos SET status='done',result='{}'")
    worker.control(run, 'retry')
    assert db.one('SELECT state FROM tasks')['state'] == 'failed'
    assert db.one('SELECT status FROM videos')['status'] == 'done'


def test_failed_directories_make_run_failed(env):
    db, ol, worker, _ = env
    run = worker.create_run('/')
    db.execute("UPDATE directories SET state='failed'")
    worker.finish_runs()
    assert db.one('SELECT state FROM runs')['state'] == 'failed'


def test_second_round_skips_known_broken_media(env):
    db, ol, worker, _ = env
    failed(env, ['/public/a.mp4'])
    recovery = Recovery(db)
    recovery.start(1, 0, rounds=2, interval=0)
    plan = recovery.status()
    plan['round'] = 2
    db.execute('UPDATE settings SET value=? WHERE `key`=?', (json.dumps(plan), KEY))
    db.execute("UPDATE videos SET error='视频容器损坏、不完整或文件内容不是有效视频'")
    assert not recovery.tick()
    assert recovery.status()['state'] == 'done'
    assert db.one('SELECT state FROM tasks')['state'] == 'failed'


def test_unavailable_mount_is_deferred_without_deleting_failures(env):
    db, ol, worker, _ = env
    failed(env, ['/offline/a.mp4','/offline-other/a.mp4','/public/a.mp4'])
    db.execute("UPDATE directories SET path='/offline/subdir'")
    recovery = Recovery(db)
    plan = recovery.start(2, 2, rounds=1, interval=0, exclude_roots=['/offline/'])
    assert plan['total_tasks'] == 2 and plan['deferred_tasks'] == 1
    assert plan['total_directories'] == 0 and plan['deferred_directories'] == 1
    assert recovery.tick()
    assert db.one("SELECT state FROM tasks WHERE path='/offline/a.mp4'")['state']=='failed'
    assert db.one("SELECT state FROM tasks WHERE path='/offline-other/a.mp4'")['state']=='queued'


def test_video_recovery_continues_while_directory_scan_is_busy(env):
    db, ol, worker, _ = env
    run=failed(env,['/public/a.mp4','/public/b.mp4','/public/c.mp4'])
    db.execute("UPDATE videos SET status='done',result='{}' WHERE path='/public/c.mp4'")
    untouched=db.one("SELECT * FROM videos WHERE path='/public/c.mp4'")
    recovery=Recovery(db);recovery.start(1,1,rounds=1,interval=0)
    directory=db.one('SELECT * FROM directories')
    plan=recovery.status();plan['current_directories']=[directory['id']]
    db.execute('UPDATE settings SET value=? WHERE `key`=?',(json.dumps(plan),KEY))
    db.execute("UPDATE directories SET state='queued'")
    db.execute("UPDATE runs SET state='running'")
    assert Recovery(db).tick()
    assert db.one("SELECT COUNT(*) AS n FROM tasks WHERE state='queued'")['n']==1
    assert Recovery(db).status()['directory_cursor']==0
    assert Recovery(db).status()['current_directories']==[directory['id']]
    assert not Recovery(db).tick()  # Still only one active video batch.
    db.execute("UPDATE tasks SET state='done' WHERE state='queued'")
    db.execute("UPDATE videos SET status='done' WHERE status='pending'")
    assert Recovery(db).tick()
    db.execute("UPDATE tasks SET state='done' WHERE state='queued'")
    db.execute("UPDATE videos SET status='done' WHERE status='pending'")
    assert not Recovery(db).tick()
    assert Recovery(db).status()['state']=='running'  # Never finish over live directories.
    remaining=db.one('SELECT * FROM directories')
    assert remaining['page']==directory['page'] and remaining['state']=='queued'
    assert db.one("SELECT * FROM videos WHERE path='/public/c.mp4'")==untouched


def test_openlist_read_retries_and_short_probe_does_not_retry(monkeypatch):
    monkeypatch.setattr('aitags.openlist.time.sleep', lambda _: None)
    client = OpenList('https://openlist.test')
    attempts = []
    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            raise httpx.ReadTimeout('signed-url-secret', request=request)
        return httpx.Response(200, json={'code':200,'data':{'size':50}})
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert client.get('/a.mp4','private-token',timeout=60)['size'] == 50
    assert len(attempts) == 3
    attempts.clear()
    with pytest.raises(HTTPException) as error:
        client.get('/a.mp4','private-token',timeout=4)
    assert len(attempts) == 1 and 'secret' not in str(error.value.detail)
    client.close()


def test_openlist_missing_file_does_not_retry(monkeypatch):
    client=OpenList('https://openlist.test');client.client.close()
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'code':500,'message':'object not found: private path'})
    client.client=httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(HTTPException) as error:client.get('/a','token',timeout=60)
    assert error.value.status_code==404 and len(calls)==1
    assert 'private' not in error.value.detail
    client.close()


def test_model_retries_explicit_busy_but_not_auth_or_conflict(monkeypatch):
    monkeypatch.setattr('aitags.model.time.sleep',lambda _:None)
    calls=[]
    def busy(*args,**kwargs):
        calls.append(1)
        return httpx.Response(503 if len(calls)<3 else 200)
    monkeypatch.setattr(httpx,'post',busy)
    assert model_post('https://model.test').status_code==200 and len(calls)==3
    for status in [401,403,409]:
        calls.clear()
        def denied(*args,**kwargs):calls.append(1);return httpx.Response(status)
        monkeypatch.setattr(httpx,'post',denied)
        assert model_post('https://model.test').status_code==status and len(calls)==1


def test_baidu_download_user_agent_is_scoped_to_provider():
    assert media_headers('https://bjbgp01.baidupcs.com/a')['User-Agent']=='pan.baidu.com'
    assert 'User-Agent' not in media_headers('https://baidupcs.com.evil.test/a')


def test_gateway_reports_actual_http_denial_without_signed_url():
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import threading
    class Denied(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):self.send_response(403);self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Denied)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin=f'http://127.0.0.1:{server.server_port}'
    try:
        with pytest.raises(MediaError,match='HTTP 403') as error:
            with gateway(origin+'/secret-token',origin,1024,5) as (url,_):
                try:httpx.get(url,timeout=3)
                except httpx.HTTPError:pass
                raise MediaError('generic ffmpeg failure')
        assert 'secret-token' not in str(error.value)
    finally:server.shutdown();server.server_close();thread.join(2)
