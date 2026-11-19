import httpx
from fastapi import HTTPException

from aitags.covers import cover_path, save_cover, sidecar_allowed, store_sidecar
from aitags.openlist import OpenList
from aitags.worker import fingerprint
from aitags.library import filename_year
from .conftest import add_video


ADMIN = {'Authorization':'admin'}
JPEG = b'\xff\xd8test\xff\xd9'


def test_catalog_year_uses_filename_dates_and_skips_resolution_numbers():
    assert filename_year('/2020/movie.1920x1080.2025.mp4')=='2025'
    assert filename_year('/2020/record-20260107-18-07.mp4')=='2026'
    assert filename_year('/2020/wallpaper.1080x1920.mp4')=='unknown'
    assert filename_year('/2020/record-20269999.mp4')=='unknown'


def test_library_search_and_cover_follow_video_permissions_and_version(env):
    db, ol, worker, client = env
    add_video(db,worker,'/public/a.mp4',ol,['城市','旅行'])
    version = fingerprint(ol.objects['/public/a.mp4'])
    save_cover(db,'/public/a.mp4',version,None,JPEG)
    query = {'keywords':'A.MP 城','tags':['旅行'],'status':'done'}
    assert client.post('/ai/api/library',json=query).status_code==403
    items = client.post('/ai/api/library',json=query,headers=ADMIN).json()['items']
    assert [(item['path'],item['cover']) for item in items]==[('/public/a.mp4',True)]
    request = {'paths':['/public/a.mp4']}
    assert client.post('/ai/api/library/cover',json=request).status_code==403
    result = client.post('/ai/api/library/cover',json=request,headers=ADMIN)
    assert result.headers['content-type']=='image/jpeg' and result.content==JPEG
    ol.objects['/public/a.mp4']['modified']='2026-11-11'
    checks=client.app.state.catalog
    with checks.lock:
        checks.entries.clear()
    assert client.post('/ai/api/library/availability',json=request).status_code==403
    client.post('/ai/api/library/availability',json=request,headers=ADMIN)
    with checks.lock:
        pending=[future for future,_ in checks.entries.values()]
    for future in pending:
        future.result(timeout=2)
    changed=client.post('/ai/api/library/availability',json=request,headers=ADMIN).json()
    assert changed['items'][0]['availability']=='changed'
    assert client.post('/ai/api/library',json=query,headers=ADMIN).json()['items']==[]
    assert client.post('/ai/api/library/cover',json=request,headers=ADMIN).status_code==404


def test_sidecar_does_not_overwrite_and_reads_back_written_file():
    def handle(request):
        assert request.method=='PUT'
        assert request.headers['Overwrite']=='false'
        assert request.headers['File-Path']=='%2Fpublic%2Ffilm.jpg'
        assert request.content==JPEG
        return httpx.Response(200,json={'code':200})

    ol=OpenList('https://files.example.test')
    ol.client.close()
    ol.client=httpx.Client(transport=httpx.MockTransport(handle))
    try:
        ol.put_cover('/public/film.jpg',JPEG,'admin')
        ol.call=lambda *args: {'content':[],'total':0}
        observed=[]
        ol.put_cover=lambda target,*args: observed.append(target)
        ol.get=lambda target,*args: {'is_dir':False,'size':len(JPEG)}
        target=store_sidecar(ol,'/public/片.mp4','version',JPEG,'admin')
        assert target==cover_path('/public/片.mp4','version')
        assert observed==[target] and target.startswith('/public/片.mp4.aitags-cover-')
        assert target!=cover_path('/public/片.mp4','other-version')
        ol.call=lambda *args: {'content':[{'path':'/public/片.mp4'}],'total':1}
        assert not sidecar_allowed(ol,'/public/片.mp4','admin')
        assert store_sidecar(ol,'/public/片.mp4','version',JPEG,'admin') is None
        assert observed==[target]
    finally:
        ol.close()


def test_readonly_mount_keeps_private_cover(env,monkeypatch):
    db, ol, worker, client = env
    add_video(db,worker,'/public/a.mp4',ol)
    monkeypatch.setattr('aitags.worker.extract_cover',lambda *args: JPEG)
    def refuse_write(*args):
        raise HTTPException(405,'只读挂载')
    monkeypatch.setattr('aitags.postprocess.store_sidecar',refuse_write)
    worker.cover_once()
    worker.postprocess.once()
    assert db.one('SELECT sidecar,jpeg FROM covers')=={'sidecar':None,'jpeg':JPEG}


def test_admin_library_batches_directory_metadata_without_resolving_downloads(env,monkeypatch):
    db,ol,worker,client=env
    for index in range(30):
        path=f'/public/{index}.mp4'
        ol.objects[path]={**ol.objects['/public/a.mp4'],'name':f'{index}.mp4'}
        add_video(db,worker,path,ol)
    reads=[]
    listing=ol.listing
    def track(*args,**kwargs):
        reads.append(args[0])
        return listing(*args,**kwargs)
    monkeypatch.setattr(ol,'listing',track)
    ol.calls.clear()
    first=client.post('/ai/api/library',json={},headers=ADMIN).json()
    assert len(first['items'])==24
    checks=client.app.state.catalog
    with checks.lock:
        pending=[future for future,_ in checks.entries.values()]
    for future in pending:
        future.result(timeout=2)
    assert reads==['/public'] and ol.calls==[]
    second=client.post('/ai/api/library',json={'page':2},headers=ADMIN).json()
    assert len(second['items'])==6 and second['total_pages']==2 and second['total']==30
    assert not ({r['path'] for r in first['items']} & {r['path'] for r in second['items']})


def test_library_returns_index_while_offline_check_is_blocked(env,monkeypatch):
    import threading
    db,ol,worker,client=env
    add_video(db,worker,'/public/a.mp4',ol)
    started=threading.Event()
    release=threading.Event()
    reads=[]
    def offline(*args,**kwargs):
        reads.append(args[0])
        started.set()
        release.wait(3)
        raise HTTPException(502,'offline')
    monkeypatch.setattr(ol,'listing',offline)
    try:
        first=client.post('/ai/api/library',json={},headers=ADMIN).json()
        assert first['items'][0]['availability']=='checking'
        assert first['items'][0]['summary']=='真实画面简介'
        assert started.wait(1) and not release.is_set()
        release.set()
        checks=client.app.state.catalog
        with checks.lock:
            pending=[future for future,_ in checks.entries.values()]
        for future in pending:
            future.result(timeout=2)
        second=client.post('/ai/api/library',json={},headers=ADMIN).json()
        assert second['items'][0]['availability']=='unavailable'
        assert len(reads)==1
    finally:
        release.set()


def test_catalog_pages_filters_and_latest_order_exclude_failed_videos(env):
    db,ol,worker,client=env
    for index in range(31):
        path=f'/public/film-{index:02d}.2025.mp4'
        ol.objects[path]={**ol.objects['/public/a.mp4'],'name':path.split('/')[-1]}
        add_video(db,worker,path,ol,['旅行'] if index%2 else ['游戏'])
    failed='/public/failed.2026.mp4'
    ol.objects[failed]={**ol.objects['/public/a.mp4'],'name':'failed.2026.mp4'}
    add_video(db,worker,failed,ol,['旅行'])
    db.execute("UPDATE videos SET status='failed' WHERE path=?",(failed,))
    first=client.post('/ai/api/library',json={},headers=ADMIN).json()
    assert first['total']==31 and first['total_pages']==2
    assert len(first['items'])==24 and first['items'][0]['path'].endswith('film-30.2025.mp4')
    second=client.post('/ai/api/library',json={'page':2},headers=ADMIN).json()
    assert len(second['items'])==7
    assert not ({row['path'] for row in first['items']} & {row['path'] for row in second['items']})
    filtered=client.post('/ai/api/library',json={'category':'旅行','year':'2025'},headers=ADMIN).json()
    assert filtered['total']==15 and len(filtered['items'])==15
    assert all(row['status']=='done' for row in filtered['items'])
    assert client.post('/ai/api/library',json={'year':'2026'},headers=ADMIN).json()['total']==0
    assert client.post('/ai/api/library',json={'status':'failed'},headers=ADMIN).json()['items']==[]
    facets=client.get('/ai/api/library/facets',headers=ADMIN).json()
    assert facets['years']==[{'value':'2025','count':31}]
    assert {entry['value'] for entry in facets['types']}=={'旅行','游戏'}
    assert client.get('/ai/api/library/facets').status_code==403


def test_catalog_tag_aliases_and_unmarked_year_are_real_metadata(env):
    db,ol,worker,client=env
    add_video(db,worker,'/public/a.mp4',ol,['动画片','城市'])
    client.put('/ai/api/tags',headers=ADMIN,json={
        'aliases':[{'alias':'动画片','canonical':'动画'}],'favorites':[]})
    result=client.post('/ai/api/library',headers=ADMIN,json={
        'category':'动画','year':'unknown','tags':['动画片'],'keywords':'真实画面'})
    assert result.status_code==200 and result.json()['total']==1
    assert result.json()['items'][0]['year']=='unknown'
    client.post('/ai/api/videos/edit',headers=ADMIN,json={
        'path':'/public/a.mp4','manual_tags':[],'removed':['动画片']})
    assert client.post('/ai/api/library',headers=ADMIN,json={'category':'动画'}).json()['total']==0


def test_numbered_pages_keep_the_same_cutoff_while_recognition_adds_results(env):
    db,ol,worker,client=env
    for index in range(27):
        path=f'/public/{index:02d}.mp4'
        ol.objects[path]={**ol.objects['/public/a.mp4'],'name':path.split('/')[-1]}
        add_video(db,worker,path,ol)
    first=client.post('/ai/api/library',headers=ADMIN,json={}).json()
    path='/public/new.mp4'
    ol.objects[path]={**ol.objects['/public/a.mp4'],'name':'new.mp4'}
    add_video(db,worker,path,ol)
    second=client.post('/ai/api/library',headers=ADMIN,json={'page':2,'as_of':first['as_of']}).json()
    assert second['total']==27 and len(second['items'])==3
    assert not ({row['path'] for row in first['items']} & {row['path'] for row in second['items']})
    latest=client.post('/ai/api/library',headers=ADMIN,json={}).json()
    assert latest['total']==28 and latest['items'][0]['path']==path


def test_content_partitions_combine_with_filters_and_follow_manual_tags(env):
    db,ol,worker,client=env
    for name,tags in [('general',['旅行']),('adult',['nsfw','旅行']),('alias',['限制级','旅行'])]:
        path=f'/public/{name}.2025.mp4'
        ol.objects[path]={**ol.objects['/public/a.mp4'],'name':path.split('/')[-1]}
        add_video(db,worker,path,ol,tags)
    client.put('/ai/api/tags',headers=ADMIN,json={
        'aliases':[{'alias':'限制级','canonical':'成人内容'}],'favorites':[]})
    def query(content,**extra):
        response=client.post('/ai/api/library',headers=ADMIN,json={'content':content,**extra})
        assert response.status_code==200
        return response.json()
    adult=query('adult',category='旅行',year='2025',page_size=1)
    general=query('general')
    assert adult['total']==2 and adult['total_pages']==2 and general['total']==1
    second=query('adult',page=2,page_size=1,as_of=adult['as_of'])
    assert adult['items'][0]['path']!=second['items'][0]['path']
    assert query('adult',keywords='general')['total']==0
    assert query('all')['total']==adult['total']+general['total']
    client.post('/ai/api/videos/edit',headers=ADMIN,json={
        'path':'/public/adult.2025.mp4','manual_tags':[],'removed':['nsfw']})
    assert query('adult')['total']==1 and query('general')['total']==2
    client.post('/ai/api/videos/edit',headers=ADMIN,json={
        'path':'/public/general.2025.mp4','manual_tags':['成人内容'],'removed':[]})
    assert query('adult')['total']==2 and query('general')['total']==1
    assert client.post('/ai/api/library',headers=ADMIN,json={'content':'invalid'}).status_code==422
    assert client.post('/ai/api/library',json={'content':'adult'}).status_code==403


def test_standalone_cinema_shell_and_permission_scoped_catalog(env):
    db,ol,worker,client=env
    add_video(db,worker,'/public/a.mp4',ol,['旅行'])
    add_video(db,worker,'/secret/b.mp4',ol,['秘密标签'])
    admin=client.post('/ai/api/cinema',headers=ADMIN,json={}).json()
    assert admin['total']==2 and len(admin['items'])==2
    guest=client.post('/ai/api/cinema',json={}).json()
    assert guest['total'] is None and guest['total_pages'] is None
    assert [item['path'] for item in guest['items']]==['/public/a.mp4']
    assert '秘密标签' not in str(guest)
    limited=client.post('/ai/api/cinema',headers={'Authorization':'limited'},json={}).json()
    assert [item['path'] for item in limited['items']]==['/a.mp4']
    assert client.post('/ai/api/cinema',headers={'Authorization':'bad'},json={}).status_code==401
    facets=client.get('/ai/api/cinema/facets').json()
    assert not any('count' in option for option in facets['types']+facets['years'])


def test_cinema_cursors_advance_over_denied_files_and_covers_use_current_permissions(env):
    db,ol,worker,client=env
    add_video(db,worker,'/public/a.mp4',ol)
    add_video(db,worker,'/secret/b.mp4',ol,['秘密'])
    version=fingerprint(ol.objects['/public/a.mp4'])
    save_cover(db,'/public/a.mp4',version,None,JPEG)
    save_cover(db,'/secret/b.mp4',fingerprint(ol.objects['/secret/b.mp4']),None,JPEG)
    first=client.post('/ai/api/cinema',json={'page_size':1}).json()
    assert first['items']==[] and first['next_cursor']==1
    second=client.post('/ai/api/cinema',json={'page_size':1,'cursor':first['next_cursor'],'as_of':first['as_of']}).json()
    assert second['items'][0]['path']=='/public/a.mp4' and second['next_cursor'] is None
    assert client.post('/ai/api/cinema/cover',json={'paths':['/public/a.mp4']}).content==JPEG
    assert client.post('/ai/api/cinema/cover',json={'paths':['/secret/b.mp4']}).status_code==403
    assert client.post('/ai/api/library/cover',json={'paths':['/public/a.mp4']}).status_code==403
