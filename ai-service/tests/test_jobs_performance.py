def test_many_runs_keep_correct_counts_with_bounded_database_roundtrips(env,monkeypatch):
    db,ol,worker,client=env
    first=worker.create_run('/public')
    worker.enqueue('/public/a.mp4',ol.objects['/public/a.mp4'],first)
    second=worker.create_run('/secret')
    worker.enqueue('/secret/b.mp4',ol.objects['/secret/b.mp4'],second)
    db.execute("UPDATE tasks SET state='done' WHERE run_id=?",(first,))
    for index in range(80):worker.create_run('/archive/'+str(index))
    calls=[];original=db.all
    def tracked(sql,args=()):
        calls.append(sql)
        return original(sql,args)
    monkeypatch.setattr(db,'all',tracked)
    result=client.get('/ai/api/jobs',headers={'Authorization':'admin'})
    assert result.status_code==200
    rows={row['id']:row for row in result.json()}
    assert len(rows)==82
    assert rows[first]['tasks']=={'done':1}
    assert rows[second]['tasks']=={'queued':1}
    assert rows[first]['directories']=={'queued':1}
    assert len(calls)<=3


def test_health_distinguishes_staging_from_an_active_worker(env):
    _,_,_,client=env
    health=client.get('/ai/health').json()
    assert health['status']=='ok'
    assert health['worker_enabled'] is False
    assert health['worker_running'] is False
