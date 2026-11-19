import threading
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from aitags.app import create_app
from aitags.worker import Worker
from aitags.protocols.publisher import Publisher


class Polls(threading.Event):
    def __init__(self, count=3):
        super().__init__();self.count=count;self.waits=[]
    def wait(self, timeout=None):
        self.waits.append(timeout)
        if len(self.waits)>=self.count:self.set()
        return self.is_set()


@pytest.mark.parametrize('loop',['scan','cover','work'])
def test_background_loops_survive_settings_and_history_outage(env,monkeypatch,caplog,loop):
    db,ol,w,_=env;original=db.settings;reads=[];work=[]
    def settings():
        reads.append(1)
        if len(reads)==2:raise RuntimeError('PRIVATE_DATABASE_DSN')
        return {**original(),'cover_backfill':True}
    monkeypatch.setattr(db,'settings',settings)
    monkeypatch.setattr(db,'event',lambda *a:(_ for _ in ()).throw(RuntimeError('PRIVATE_DATABASE_DSN')))
    w.stop=Polls()
    if loop=='scan':
        monkeypatch.setattr(w.recovery,'tick',lambda:None)
        monkeypatch.setattr(w,'scan_once',lambda:work.append(1) or True)
        monkeypatch.setattr(w,'finish_runs',lambda:None)
        w.scan_loop()
    elif loop=='cover':
        monkeypatch.setattr(w,'cover_once',lambda:work.append(1))
        w.cover_loop()
    else:
        # Force a settings refresh on each iteration, without sleeping in the test.
        class RefreshLock:
            def __enter__(self):w.concurrency_checked=float('-inf')
            def __exit__(self,*a):pass
        w.concurrency_lock=RefreshLock()
        monkeypatch.setattr(w,'work_once',lambda:work.append(1) or True)
        w.work_loop(0)
    assert len(work)==2 and len(w.stop.waits)==3
    assert 'PRIVATE_DATABASE_DSN' not in caplog.text
    assert 'History temporarily unavailable' in caplog.text


def test_publisher_retries_after_history_write_also_fails(env,monkeypatch,caplog):
    db,ol,w,client=env;publisher=client.app.state.publisher;calls=[]
    publisher.stop=Polls(10)
    def once():
        calls.append(1)
        if len(calls)==1:raise RuntimeError('PRIVATE_DATABASE_DSN')
        publisher.stop.set();return True
    monkeypatch.setattr(publisher,'once',once)
    monkeypatch.setattr(db,'event',lambda *a:(_ for _ in ()).throw(RuntimeError('PRIVATE_DATABASE_DSN')))
    publisher.start();publisher.thread.join(3)
    assert not publisher.thread.is_alive() and len(calls)==2
    assert 'PRIVATE_DATABASE_DSN' not in caplog.text


def test_committed_recognition_is_not_failed_by_history_outage(env,monkeypatch):
    db,ol,w,_=env
    rid=w.create_run('/public');w.enqueue('/public/a.mp4',ol.objects['/public/a.mp4'],rid)
    monkeypatch.setattr('aitags.worker.compress_video',lambda *a,**kw:{})
    monkeypatch.setattr('aitags.worker.infer',lambda *a:{'summary':'真实结果','tags':['已确认']})
    monkeypatch.setattr(db,'event',lambda *a:(_ for _ in ()).throw(RuntimeError('History table unavailable')))
    assert w.work_once()
    assert db.one('SELECT state FROM tasks')['state']=='done'
    assert db.one('SELECT status FROM videos')['status']=='done'
    assert db.one('SELECT COUNT(*) AS n FROM postprocess_jobs')['n']==1


def test_health_reports_dead_worker_or_publisher_as_unavailable(env,monkeypatch):
    db,ol,_,_=env
    class ThreadState:
        alive=True
        def is_alive(self):return self.alive
    monkeypatch.setattr(Worker,'start',lambda self:setattr(self,'threads',[ThreadState()]))
    monkeypatch.setattr(Worker,'close',lambda self:None)
    monkeypatch.setattr(Publisher,'start',lambda self:setattr(self,'thread',ThreadState()))
    monkeypatch.setattr(Publisher,'close',lambda self:None)
    app=create_app(db,ol,True)
    with TestClient(app) as client:
        assert client.get('/ai/health').status_code==200
        app.state.worker.threads[0].alive=False
        r=client.get('/ai/health');assert r.status_code==503 and r.json()['status']=='degraded'
        app.state.worker.threads[0].alive=True
        app.state.publisher.thread.alive=False
        r=client.get('/ai/health');assert r.status_code==503 and not r.json()['publisher_running']
