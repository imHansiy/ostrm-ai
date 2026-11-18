import json
from .conftest import add_video


def test_scope_counts_are_literal_and_not_queue_counts(env):
    db,ol,w,client=env
    add_video(db,w,'/public/a.mp4',ol)
    run=w.create_run('/secret');w.enqueue('/secret/b.mp4',ol.objects['/secret/b.mp4'],run)
    db.execute("UPDATE videos SET status='failed' WHERE path='/secret/b.mp4'")
    db.execute("UPDATE tasks SET state='failed' WHERE path='/secret/b.mp4'")
    db.save_settings({'failure_recovery':{'state':'running','exclude_roots':['/secret'],'task_ids':[1,2],'directory_ids':[],'task_cursor':0}})
    r=client.get('/ai/api/overview',headers={'Authorization':'admin'}).json()
    assert r['videos']=={'done':1,'failed':1} and r['tasks']=={}
    assert r['scopes']['included']['videos']=={'done':1}
    assert r['scopes']['deferred']['videos']=={'failed':1}
    assert r['directories']['queued']==2 and r['background']['pending']==0
    assert r['recovery']['total_tasks']==2 and 'task_ids' not in r['recovery']
    assert r['generated_at']>0


def test_scope_prefix_does_not_treat_percent_as_wildcard(env):
    db,ol,w,client=env
    for path in ('/a_%/x.mp4','/abbb/y.mp4','/a_%extra/z.mp4'):
        ol.objects[path]={**ol.objects['/public/a.mp4'],'name':path.rsplit('/',1)[1]}
        add_video(db,w,path,ol)
    db.save_settings({'failure_recovery':{'state':'paused','exclude_roots':['/a_%'],'task_ids':[],'directory_ids':[]}})
    r=client.get('/ai/api/overview',headers={'Authorization':'admin'}).json()
    assert r['scopes']['deferred']['videos']=={'done':1}
    assert r['scopes']['included']['videos']=={'done':2}


def test_paused_directories_and_background_retries_remain_visible(env):
    db,ol,w,client=env
    run=add_video(db,w,'/public/a.mp4',ol);w.control(run,'pause')
    db.execute("INSERT INTO postprocess_jobs(path,fingerprint,created,attempts) VALUES ('/public/a.mp4','version',1,3)")
    r=client.get('/ai/api/overview',headers={'Authorization':'admin'}).json()
    assert r['directories']=={'paused':1}
    assert r['background']=={'pending':1,'retrying':1}
    assert r['tasks']=={}
    assert client.get('/ai/api/overview').status_code==403
