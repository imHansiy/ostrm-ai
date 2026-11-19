import httpx
from urllib.request import build_opener, ProxyHandler
from aitags import config, media
from aitags.openlist import OpenList


def test_driver_link_credentials_are_only_in_worker_resolution(monkeypatch):
    monkeypatch.setattr(config, 'MEDIA_DIRECT_DRIVERS', ['Quark'])
    monkeypatch.setattr(config, 'OPENLIST_PASSWORD', '')
    calls=[]
    def respond(request):
        calls.append((request.url.path,request.headers.get('Authorization')))
        if request.url.path=='/api/fs/get':
            data={'provider':'Quark','raw_url':'https://files.test/p/movie.mp4','size':20}
        else:
            data={'url':'https://cdn.test/movie.mp4','header':{
                'Cookie':['driver-cookie'],'User-Agent':['driver-agent'],
                'Proxy-Authorization':['must-not-forward'],'Referer':['bad\r\nheader']}}
        return httpx.Response(200,json={'code':200,'data':data})
    ol=OpenList('https://files.test');ol.client.close()
    ol.client=httpx.Client(transport=httpx.MockTransport(respond));ol.token='server-admin'
    try:
        public=ol.get('/movie.mp4','user-session')
        assert public['raw_url']=='https://files.test/p/movie.mp4'
        assert '_media_headers' not in public
        private=ol.get_media('/movie.mp4')
        assert private['raw_url']=='https://cdn.test/movie.mp4'
        assert private['_media_headers']=={'Cookie':'driver-cookie','User-Agent':'driver-agent'}
        assert calls[-1]==('/api/fs/link','server-admin')
    finally:ol.close()


def test_worker_direct_resolution_is_opt_in_per_driver(monkeypatch):
    monkeypatch.setattr(config,'MEDIA_DIRECT_DRIVERS',[])
    ol=OpenList('https://files.test')
    monkeypatch.setattr(ol,'admin_token',lambda:'server-admin')
    monkeypatch.setattr(ol,'get',lambda *a,**kw:{'provider':'Quark','raw_url':'https://files.test/p/file'})
    monkeypatch.setattr(ol,'call',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('no private link needed')))
    try:assert '_media_headers' not in ol.get_media('/file')
    finally:ol.close()


def test_driver_credentials_do_not_cross_redirect_origins(monkeypatch):
    real_client=httpx.Client;seen=[]
    def respond(request):
        seen.append(request)
        if request.url.host=='source.test':
            return httpx.Response(302,headers={'Location':'https://other.test/movie'})
        return httpx.Response(200,headers={'Content-Length':'5'},stream=httpx.ByteStream(b'video'))
    monkeypatch.setattr(media,'approve_url',lambda *a:None)
    monkeypatch.setattr(config,'MEDIA_PROXY_URL','')
    monkeypatch.setattr(media.httpx,'Client',lambda **kw:real_client(transport=httpx.MockTransport(respond),**kw))
    with media.gateway('https://source.test/movie','http://openlist',100,5,
                       request_headers={'Cookie':'private','Authorization':'Bearer private'}) as (url,used):
        with build_opener(ProxyHandler({})).open(url,timeout=3) as response:
            assert response.read()==b'video'
    assert seen[0].headers['Cookie']=='private'
    assert seen[0].headers['Authorization']=='Bearer private'
    assert 'Cookie' not in seen[1].headers
    assert 'Authorization' not in seen[1].headers
