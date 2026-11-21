import httpx
import pytest
from fastapi import HTTPException
from aitags import config
from aitags.openlist import OpenList


@pytest.mark.parametrize('url,role,disabled,allowed',[
    ('http://127.0.0.1:43646',2,False,True),
    ('http://[::1]:43646',2,False,True),
    ('http://127.0.0.1:43646',0,False,False),
    ('http://127.0.0.1:43646',2,True,False),
    ('https://files.example.test',2,False,False),
])
def test_cached_local_auth_requires_existing_openlist_admin(monkeypatch,url,role,disabled,allowed):
    monkeypatch.setattr(config,'OPENLIST_LOCAL_CACHED_ADMIN',True)
    ol=OpenList(url);ol.client.close();seen=[]
    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200,json={'code':200,'data':{'role':role,'disabled':disabled}})
    ol.client=httpx.Client(transport=httpx.MockTransport(respond))
    try:
        if allowed:assert ol.admin_token()==''
        else:
            with pytest.raises(HTTPException):ol.admin_token()
        assert all(path=='/api/me' for path in seen)
        if 'example.test' in url:assert not seen
    finally:ol.close()


def test_restore_deferred_video_cannot_be_enqueued_even_by_force(env):
    db,ol,w,client=env
    db.save_settings({'restore_deferred_successes':{'/public/a.mp4':'remote-result-hash'}})
    rid=w.create_run('/public')
    assert not w.enqueue('/public/a.mp4',ol.objects['/public/a.mp4'],rid,True)
    assert db.one('SELECT COUNT(*) AS n FROM tasks')['n']==0
    assert w.excluded('/public/a.mp4',db.settings())


def test_stale_queued_success_is_skipped_without_reading_media(env,monkeypatch):
    db,ol,w,client=env
    rid=w.create_run('/public');w.enqueue('/public/a.mp4',ol.objects['/public/a.mp4'],rid)
    db.save_settings({'restore_deferred_successes':{'/public/a.mp4':'remote-result-hash'}})
    monkeypatch.setattr(ol,'get',lambda *args,**kwargs:pytest.fail('Deferred video was read'))
    assert w.work_once()
    assert db.one('SELECT state FROM tasks')['state']=='cancelled'
    assert db.one('SELECT status FROM videos')['status']!='done'
    assert db.one('SELECT attempts FROM tasks')['attempts']==0


def test_restore_mode_does_not_expose_cached_admin_to_anonymous_browser(env,monkeypatch):
    db,ol,w,client=env
    monkeypatch.setattr(config,'OPENLIST_LOCAL_CACHED_ADMIN',True)
    monkeypatch.setattr(ol,'me',lambda token:{'role':2,'base_path':'/','disabled':False})
    monkeypatch.setenv('AI_BRIDGE_KEY','test-private-server-bridge')
    assert client.get('/ai/api/settings').status_code==401
    assert client.get('/ai/api/settings',headers={'X-AI-Bridge':'wrong'}).status_code==401
    assert client.get('/ai/api/settings',headers={'X-AI-Bridge':'test-private-server-bridge'}).status_code==200
