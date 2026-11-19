import time
import httpx
import pytest
from fastapi import HTTPException
from aitags import config
from aitags.openlist import OpenList


@pytest.mark.parametrize('http_status',[200,401])
def test_expired_backend_admin_read_refreshes_once(monkeypatch,http_status):
    monkeypatch.setattr(config,'OPENLIST_PASSWORD','test-password')
    seen=[]
    def respond(request):
        seen.append((request.url.path,request.headers.get('Authorization')))
        if request.url.path=='/api/auth/login/hash':
            return httpx.Response(200,json={'code':200,'data':{'token':'fresh-admin'}})
        if request.headers.get('Authorization')=='old-admin':
            return httpx.Response(http_status,json={'code':401,'message':'expired'})
        return httpx.Response(200,json={'code':200,'data':{'name':'movie.mp4'}})
    ol=OpenList('https://files.test');ol.client.close()
    ol.client=httpx.Client(transport=httpx.MockTransport(respond));ol.token='old-admin';ol.login_time=time.time()
    try:
        assert ol.get('/movie.mp4',ol.admin_token())['name']=='movie.mp4'
        assert seen==[('/api/fs/get','old-admin'),('/api/auth/login/hash',''),('/api/fs/get','fresh-admin')]
    finally:ol.close()


def test_user_token_is_never_upgraded_to_backend_admin(monkeypatch):
    monkeypatch.setattr(config,'OPENLIST_PASSWORD','test-password')
    seen=[]
    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200,json={'code':401})
    ol=OpenList('https://files.test');ol.client.close()
    ol.client=httpx.Client(transport=httpx.MockTransport(respond));ol.token='backend-admin';ol.login_time=time.time()
    try:
        with pytest.raises(HTTPException) as error:ol.get('/secret','ordinary-user')
        assert error.value.status_code==401
        assert seen==['/api/fs/get']
    finally:ol.close()


def test_invalid_admin_credentials_do_not_loop(monkeypatch):
    monkeypatch.setattr(config,'OPENLIST_PASSWORD','test-password')
    calls=[]
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200,json={'code':401})
    ol=OpenList('https://files.test');ol.client.close()
    ol.client=httpx.Client(transport=httpx.MockTransport(respond));ol.token='backend-admin';ol.login_time=time.time()
    try:
        with pytest.raises(HTTPException):ol.get('/movie',ol.admin_token())
        assert calls==['/api/fs/get','/api/auth/login/hash']
    finally:ol.close()
