import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from aitags.worker import Worker
from aitags.db import Database
from aitags.storage import copy_database


def jobs(env):
    db, ol, w, _ = env
    rid=w.create_run('/public')
    for name in ('a','next'):
        path=f'/public/{name}.mp4'
        if path not in ol.objects:
            ol.objects[path]={**ol.objects['/public/a.mp4'],'name':name+'.mp4','raw_url':'https://media.example.test/'+name+'.mp4'}
        w.enqueue(path,ol.objects[path],rid)
    return db,ol,w,rid


def test_prepares_next_while_model_busy_but_bounds_stages(env,monkeypatch):
    db,ol,w,rid=jobs(env)
    model_entered=threading.Event();next_prepared=threading.Event();release=threading.Event()
    calls=[];gate=threading.Lock();active=[0];maximum=[0]
    sample={'video':'AAAA','video_bytes':3,'duration':1,'network_bytes':3}
    def compress(url,*args,**kw):
        with gate:
            active[0]+=1;maximum[0]=max(maximum[0],active[0])
        if 'next' in url:
            assert model_entered.wait(3)
            next_prepared.set()
        with gate:active[0]-=1
        return dict(sample)
    def infer(evidence,settings):
        calls.append(dict(evidence))
        if len(calls)==1:
            model_entered.set();assert release.wait(5)
        return {'summary':'原始结果','tags':['原始标签']}
    monkeypatch.setattr('aitags.worker.compress_video',compress)
    monkeypatch.setattr('aitags.worker.infer',infer)
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(w.work_once)
        assert model_entered.wait(3)
        second=pool.submit(w.work_once)
        try:
            assert next_prepared.wait(3)
            # The second video is ready, but only one model request is admitted.
            assert len(calls)==1 and not first.done()
            assert len(w.activity.snapshot()['active'])==2
        finally:release.set()
        assert first.result(5) and second.result(5)
    assert maximum[0]==1 and len(calls)==2
    assert all(all(row[k]==v for k,v in sample.items()) for row in calls)
    assert db.one("SELECT COUNT(*) AS n FROM videos WHERE status='done'")['n']==2
    assert db.one('SELECT COUNT(*) AS n FROM postprocess_jobs')['n']==2
    assert not w.activity.snapshot()['active']


def test_publish_is_durable_and_does_not_block_recognition(env,monkeypatch,tmp_path):
    db,ol,w,rid=jobs(env)
    monkeypatch.setattr('aitags.worker.compress_video',lambda *a,**kw:{})
    monkeypatch.setattr('aitags.worker.infer',lambda *a:{'summary':'原结果','tags':['AI']})
    w.on_result=lambda path:(_ for _ in ()).throw(RuntimeError('publish unavailable'))
    assert w.work_once()
    assert db.one("SELECT state FROM tasks WHERE path='/public/a.mp4'")['state']=='done'
    assert db.one('SELECT attempts FROM postprocess_jobs')['attempts']==0
    assert w.postprocess.once()
    assert db.one('SELECT attempts FROM postprocess_jobs')['attempts']==1
    # A restart and a full database backup preserve unfinished publication work.
    copy=Database(tmp_path/'copy.sqlite3');copy_database(db,copy)
    assert copy.one('SELECT attempts FROM postprocess_jobs')['attempts']==1
    restarted=Worker(db,ol);published=[];restarted.on_result=published.append
    db.execute('UPDATE postprocess_jobs SET next_attempt=0')
    assert restarted.postprocess.once()
    assert published==['/public/a.mp4']
    assert not db.one('SELECT * FROM postprocess_jobs')
    assert db.one("SELECT status FROM videos WHERE path='/public/a.mp4'")['status']=='done'


def test_cancel_prepared_video_before_model(env,monkeypatch):
    db,ol,w,rid=jobs(env)
    entered=threading.Event();release=threading.Event();calls=[];prepared=threading.Event()
    def compress(url,*a,**kw):
        if 'next' in url:prepared.set()
        return {}
    def infer(*args):
        calls.append(1);entered.set();assert release.wait(5)
        return {'summary':'必须丢弃','tags':[]}
    monkeypatch.setattr('aitags.worker.compress_video',compress)
    monkeypatch.setattr('aitags.worker.infer',infer)
    with ThreadPoolExecutor(2) as pool:
        one=pool.submit(w.work_once);assert entered.wait(3)
        two=pool.submit(w.work_once)
        try:
            assert prepared.wait(3);w.control(rid,'cancel')
        finally:release.set()
        one.result(5);two.result(5)
    assert len(calls)==1
    assert all(r['state']=='cancelled' for r in db.all('SELECT state FROM tasks'))
    assert not db.one('SELECT * FROM postprocess_jobs')
    assert all(r['result']=='{}' for r in db.all('SELECT result FROM videos'))


def test_stop_requeues_prepared_and_inference_tasks(env,monkeypatch):
    db,ol,w,rid=jobs(env)
    entered=threading.Event();release=threading.Event();prepared=threading.Event()
    def compress(url,*a,**kw):
        if 'next' in url:prepared.set()
        return {}
    def infer(*a):
        entered.set();assert release.wait(5);return {'summary':'不要提交','tags':[]}
    monkeypatch.setattr('aitags.worker.compress_video',compress)
    monkeypatch.setattr('aitags.worker.infer',infer)
    with ThreadPoolExecutor(2) as pool:
        one=pool.submit(w.work_once);assert entered.wait(3)
        two=pool.submit(w.work_once)
        try:
            assert prepared.wait(3);w.stop.set()
        finally:release.set()
        one.result(5);two.result(5)
    db.recover()
    assert all(r['state']=='queued' for r in db.all('SELECT state FROM tasks'))
    assert w.stage_counts=={'prepare':0,'model':0}
    assert not w.activity.snapshot()['active']


def test_pause_stops_claims_but_drains_claimed_task(env,monkeypatch):
    db,ol,w,rid=jobs(env)
    monkeypatch.setattr('aitags.worker.compress_video',lambda *a,**kw:{})
    def infer(*args):
        w.control(rid,'pause');return {'summary':'完成当前任务','tags':[]}
    monkeypatch.setattr('aitags.worker.infer',infer)
    assert w.work_once() and not w.work_once()
    assert db.one("SELECT COUNT(*) AS n FROM tasks WHERE state='queued'")['n']==1
    w.control(rid,'resume');assert w.work_once()


def test_cover_failure_does_not_block_protocol_or_lose_local_cover(env,monkeypatch):
    from aitags.covers import cached_cover
    db,ol,w,rid=jobs(env);jpeg=b'\xff\xd8fake-jpeg\xff\xd9'
    monkeypatch.setattr('aitags.worker.compress_video',lambda *a,**kw:{'cover':base64.b64encode(jpeg).decode()})
    monkeypatch.setattr('aitags.worker.infer',lambda *a:{'summary':'原结果','tags':[]})
    monkeypatch.setattr('aitags.postprocess.store_sidecar',lambda *a:(_ for _ in ()).throw(RuntimeError('upload offline')))
    published=[];w.on_result=published.append
    assert w.work_once() and w.postprocess.once()
    assert published==['/public/a.mp4']
    row=db.one("SELECT * FROM videos WHERE path='/public/a.mp4'")
    assert row['status']=='done' and cached_cover(db,row['path'],row['fingerprint'])==jpeg
    assert db.one('SELECT attempts FROM postprocess_jobs')['attempts']==1
    monkeypatch.setattr('aitags.postprocess.store_sidecar',lambda *a:'/public/cover.jpg')
    db.execute('UPDATE postprocess_jobs SET next_attempt=0');w.postprocess.once()
    assert not db.one('SELECT * FROM postprocess_jobs')


def test_stale_background_job_is_discarded(env,monkeypatch):
    db,ol,w,rid=jobs(env)
    monkeypatch.setattr('aitags.worker.compress_video',lambda *a,**kw:{})
    monkeypatch.setattr('aitags.worker.infer',lambda *a:{'summary':'原结果','tags':[]})
    assert w.work_once()
    db.execute("UPDATE videos SET fingerprint='changed' WHERE path='/public/a.mp4'")
    published=[];w.on_result=published.append
    assert w.postprocess.once() and not published
    assert not db.one('SELECT * FROM postprocess_jobs')
