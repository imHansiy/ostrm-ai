import json,time,httpx,pytest
from aitags.recovery import Recovery,KEY
from aitags.model import ModelRoutingError,infer
from aitags.config import DEFAULTS
from .test_recovery import failed


def test_global_model_route_error_is_distinguished_without_exposing_body(monkeypatch):
    response=httpx.Response(409,request=httpx.Request('POST','https://api.modelbest.ai/v1/chat/completions'),
        json={'error':{'type':'lis_global_ambiguous_model','message':'private text'}})
    monkeypatch.setattr('aitags.model.model_post',lambda *a,**k:response)
    with pytest.raises(ModelRoutingError) as error:
        infer({'duration':1,'video':'AAAA','video_bytes':3,'network_bytes':0},DEFAULTS,key='private')
    assert 'private' not in str(error.value)


def test_hold_survives_restart_stops_downloads_and_heals_before_same_task(env,monkeypatch):
    db,ol,worker,_=env
    failed(env,['/public/a.mp4'])
    recovery=Recovery(db);recovery.start(1,0,rounds=1,interval=0);assert recovery.tick()
    monkeypatch.setattr('aitags.worker.compress_video',lambda *a,**k:{'video':'AAAA','video_bytes':3,'duration':1})
    def unavailable(*a,**k):raise ModelRoutingError('model route unavailable')
    monkeypatch.setattr('aitags.worker.infer',unavailable)
    assert worker.work_once()
    plan=Recovery(db).status();assert plan['model_hold']['reason']=='lis_global_ambiguous_model'
    assert db.one('SELECT state,attempts FROM tasks')=={'state':'queued','attempts':1}
    assert not worker.work_once() and not Recovery(db).tick()
    plan['model_hold']['next_check_at']=0;db.execute('UPDATE settings SET value=? WHERE `key`=?',(json.dumps(plan),KEY))
    requests=[]
    def probe(*a,**k):
        requests.append(k['json'])
        return httpx.Response(200,json={'choices':[{'message':{'content':'正常'}}]})
    monkeypatch.setattr('aitags.recovery.httpx.post',probe)
    Recovery(db).tick();assert 'model_hold' not in Recovery(db).status()
    assert len(requests)==1 and 'video_url' not in json.dumps(requests)
    monkeypatch.setattr('aitags.worker.infer',lambda *a,**k:{'summary':'recognized','tags':['scene']})
    assert worker.work_once() and db.one('SELECT state,attempts FROM tasks')=={'state':'done','attempts':2}


def test_failed_health_probe_is_bounded_and_preserves_all_queue_rows(env,monkeypatch):
    db,ol,worker,_=env
    failed(env,['/public/a.mp4']);recovery=Recovery(db);recovery.start(1,0,rounds=1,interval=0)
    plan=recovery.status();plan['model_hold']={'reason':'lis_global_ambiguous_model','next_check_at':0,'checks':0}
    db.execute('UPDATE settings SET value=? WHERE `key`=?',(json.dumps(plan),KEY))
    before=db.all('SELECT * FROM tasks');requests=[]
    def unavailable(*a,**k):requests.append(1);return httpx.Response(409,json={'error':{'type':'lis_global_ambiguous_model'}})
    monkeypatch.setattr('aitags.recovery.httpx.post',unavailable)
    assert not Recovery(db).tick() and not Recovery(db).tick()
    assert len(requests)==1 and db.all('SELECT * FROM tasks')==before
    assert Recovery(db).status()['model_hold']['next_check_at']>time.time()+290
