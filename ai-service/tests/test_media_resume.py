import time
import httpx
import pytest
from aitags.media import iter_media_body, MediaError


def test_initial_get_connection_failure_is_retried_once(monkeypatch):
    from aitags.media import _open_media_response
    monkeypatch.setattr('aitags.media._callback_wait',lambda *args:None)
    requests=[]
    def respond(request):
        requests.append(request)
        if len(requests)==1:raise httpx.ConnectError('temporary')
        return httpx.Response(200,stream=httpx.ByteStream(b'ok'))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        response=_open_media_response(client,'GET','https://source.test/video',{},time.monotonic()+10,lambda:False)
        assert response.read()==b'ok' and len(requests)==2


def test_initial_connection_retries_are_bounded(monkeypatch):
    from aitags.media import _open_media_response
    monkeypatch.setattr('aitags.media._callback_wait',lambda *args:None)
    requests=[]
    def fail(request):requests.append(request);raise httpx.ReadTimeout('temporary')
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(httpx.ReadTimeout):
            _open_media_response(client,'GET','https://source.test/video',{},time.monotonic()+10,lambda:False)
    assert len(requests)==2


class Interrupted(httpx.SyncByteStream):
    def __iter__(self):
        yield b'a'*32768
        raise httpx.ReadError('connection reset with private URL')


def test_resume_retains_exact_bytes_and_validates_entity(monkeypatch):
    monkeypatch.setattr('aitags.media._callback_wait', lambda *args: None)
    requests=[]
    def respond(request):
        requests.append(request)
        if len(requests)==1:
            return httpx.Response(200,headers={'Content-Length':'65536','Accept-Ranges':'bytes','ETag':'"stable"'},stream=Interrupted())
        assert request.headers['Range']=='bytes=32768-65535'
        assert request.headers['If-Range']=='"stable"'
        assert request.headers['Authorization']=='private'
        return httpx.Response(206,headers={'Content-Range':'bytes 32768-65535/65536','ETag':'"stable"'},stream=httpx.ByteStream(b'b'*32768))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        response=client.send(client.build_request('GET','https://source.test/video'),stream=True)
        data=b''.join(iter_media_body(client,response,'https://source.test/video',{'Authorization':'private'},time.monotonic()+10,lambda:False))
        assert data==b'a'*32768+b'b'*32768 and len(requests)==2 and response.is_closed


@pytest.mark.parametrize('change', ['etag','total','offset','status','encoding'])
def test_resume_rejects_replaced_or_misaligned_content(monkeypatch,change):
    monkeypatch.setattr('aitags.media._callback_wait', lambda *args:None)
    original=httpx.Response(206,headers={'Content-Range':'bytes 0-65535/100000','ETag':'"stable"'},stream=Interrupted())
    headers={'Content-Range':'bytes 32768-65535/100000','ETag':'"stable"'}
    if change=='etag':headers['ETag']='"changed"'
    if change=='total':headers['Content-Range']='bytes 32768-65535/200000'
    if change=='offset':headers['Content-Range']='bytes 0-65535/100000'
    if change=='encoding':headers['Content-Encoding']='gzip'
    resumed=[]
    def respond(request):
        response=httpx.Response(200 if change=='status' else 206,headers=headers,stream=httpx.ByteStream(b'b'*32768))
        resumed.append(response);return response
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MediaError,match='安全续传'):
            list(iter_media_body(client,original,'https://source.test/video',{},time.monotonic()+10,lambda:False))
    assert resumed[0].is_closed


def test_missing_validator_never_retries():
    response=httpx.Response(200,headers={'Content-Length':'65536','Accept-Ranges':'bytes'},stream=Interrupted())
    with httpx.Client(transport=httpx.MockTransport(lambda _:pytest.fail('must not resume without entity validator'))) as client:
        with pytest.raises(MediaError) as error:
            list(iter_media_body(client,response,'https://source.test/private',{},time.monotonic()+10,lambda:False))
    assert 'private' not in str(error.value) and response.is_closed


def test_resumption_attempts_are_bounded(monkeypatch):
    monkeypatch.setattr('aitags.media._callback_wait',lambda *args:None)
    requests=[]
    def respond(request):
        start=int(request.headers.get('Range','bytes=0-').split('=')[1].split('-')[0])
        requests.append(start)
        return httpx.Response(206,headers={'Content-Range':f'bytes {start}-999999/1000000','ETag':'"stable"'},stream=Interrupted())
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        response=client.send(client.build_request('GET','https://source.test/video'),stream=True)
        with pytest.raises(MediaError,match='有限重试'):
            list(iter_media_body(client,response,'https://source.test/video',{},time.monotonic()+10,lambda:False))
    assert requests==[0,32768,65536,98304]


def test_real_gateway_recovers_dropped_tcp_body_without_duplicate_bytes(monkeypatch):
    import re,socket,threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from aitags.media import gateway
    data=bytes(range(256))*1024
    requests=[]
    class Origin(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_GET(self):
            value=self.headers.get('Range','')
            match=re.fullmatch(r'bytes=(\d+)-(\d+)',value)
            start=int(match[1]) if match else 0
            requests.append(value)
            self.send_response(206 if match else 200)
            self.send_header('Content-Length',str(len(data)-start))
            self.send_header('Accept-Ranges','bytes');self.send_header('ETag','"unchanged"')
            if match:self.send_header('Content-Range',f'bytes {start}-{len(data)-1}/{len(data)}')
            self.end_headers()
            if not match:
                self.wfile.write(data[:65536]);self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
            else:
                assert self.headers['If-Range']=='"unchanged"'
                self.wfile.write(data[start:])
    server=ThreadingHTTPServer(('127.0.0.1',0),Origin)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setattr('aitags.media._callback_wait',lambda *args:None)
    try:
        with gateway(url+'/video',url,len(data),10) as (source,used):
            with httpx.Client(trust_env=False) as client:
                response=client.get(source)
            assert response.content==data and used[0]==len(data)
            assert requests==['','bytes=65536-262143']
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
