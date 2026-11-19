import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import build_opener, ProxyHandler
import httpx
import pytest
from aitags import config, media


def test_only_verified_callback_capacity_is_retried(monkeypatch):
    calls=[]
    def respond(request):
        calls.append(request)
        if len(calls)<3:
            return httpx.Response(403,stream=httpx.ByteStream(b'{"code":"RequestDeniedByCallback","message":"ExceedMaxConcurrency"}'))
        return httpx.Response(206,stream=httpx.ByteStream(b'video'))
    monkeypatch.setattr(media,'_callback_wait',lambda *a:None)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        response=media._open_media_response(client,'GET','https://dl1.aliyundrive.cloud/movie?token=private',{},time.monotonic()+5,lambda:False)
        assert response.status_code==206 and response.read()==b'video'
        response.close()
    assert len(calls)==3


@pytest.mark.parametrize('body',[b'AccessDenied',b'RequestDeniedByCallback',b'ExceedMaxConcurrency'])
def test_permanent_forbidden_is_not_retried(body):
    calls=[]
    def respond(request):
        calls.append(request)
        return httpx.Response(403,stream=httpx.ByteStream(body))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        response=media._open_media_response(client,'GET','https://dl1.aliyundrive.cloud/movie',{},time.monotonic()+5,lambda:False)
        assert response.status_code==403
        response.close()
    assert len(calls)==1


def test_capacity_exhaustion_is_bounded_and_does_not_leak_url(monkeypatch):
    calls=[];responses=[]
    def respond(request):
        calls.append(request)
        response=httpx.Response(403,stream=httpx.ByteStream(b'RequestDeniedByCallback ExceedMaxConcurrency'))
        responses.append(response)
        return response
    monkeypatch.setattr(media,'_callback_wait',lambda *a:None)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(media.MediaError) as error:
            media._open_media_response(client,'GET','https://dl1.aliyundrive.cloud/movie?token=private',{},time.monotonic()+5,lambda:False)
    assert len(calls)==3 and all(response.is_closed for response in responses)
    assert 'private' not in str(error.value) and 'HTTP 403' not in str(error.value)


def test_callback_slot_is_held_until_body_close_across_gateways(monkeypatch):
    real_client=httpx.Client;entered=threading.Event();release=threading.Event()
    lock=threading.Lock();active=0;maximum=0;requests=0
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield b'video'
            assert release.wait(3)
        def close(self):
            nonlocal active
            with lock:active-=1
    def respond(request):
        nonlocal active,maximum,requests
        with lock:
            requests+=1;active+=1;maximum=max(maximum,active)
        entered.set()
        return httpx.Response(200,headers={'Content-Length':'5'},stream=Body())
    monkeypatch.setattr(media,'approve_url',lambda *a:None)
    monkeypatch.setattr(config,'MEDIA_PROXY_URL','')
    monkeypatch.setattr(media.httpx,'Client',lambda **kw:real_client(transport=httpx.MockTransport(respond),**kw))
    def download():
        with media.gateway('https://dl1.aliyundrive.cloud/movie','http://openlist',100,5) as (url,used):
            with build_opener(ProxyHandler({})).open(url,timeout=4) as response:
                return response.read()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(download)
        assert entered.wait(2)
        second=pool.submit(download)
        try:
            time.sleep(.15)
            assert requests==1
        finally:release.set()
        assert first.result(timeout=4)==b'video'
        assert second.result(timeout=4)==b'video'
    assert maximum==1 and active==0 and requests==2
