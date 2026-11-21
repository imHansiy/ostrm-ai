import json,time,re
import httpx,pytest
from aitags.media_relay import MediaRelayTransport


def relay_client(respond,**kwargs):
    relay=MediaRelayTransport('https://relay.test/media-fetch','private-owner-token',['dl*.aliyundrive.cloud'],time.monotonic()+10,
                              transport=httpx.MockTransport(respond),**kwargs)
    return httpx.Client(transport=relay)


@pytest.mark.parametrize('limited',[False,True])
def test_verified_bounded_ranges_expose_one_complete_http_entity(limited):
    data=bytes(range(256))*4096;calls=[]
    def respond(request):
        payload=json.loads(request.content)
        assert request.headers['Authorization']=='Bearer private-owner-token'
        assert request.headers['Accept-Encoding']=='identity'
        assert payload['headers'].get('cookie')=='private-drive-cookie'
        assert 'if-range' not in payload['headers']
        match=re.fullmatch(r'bytes=(\d+)-(\d+)',payload['headers']['range']);start,end=map(int,match.groups())
        end=min(end,len(data)-1,start+65535) if limited else min(end,len(data)-1)
        calls.append((start,end))
        return httpx.Response(206,headers={'Content-Range':f'bytes {start}-{end}/{len(data)}','ETag':'"stable"'},stream=httpx.ByteStream(data[start:end+1]))
    with relay_client(respond) as client:
        response=client.get('https://dl1.aliyundrive.cloud/video',headers={'Cookie':'private-drive-cookie','If-Range':'old-validator'})
        assert response.status_code==200 and response.headers['Content-Length']==str(len(data))
        assert response.content==data and calls[0][0]==0
        assert all(previous[1]+1==current[0] for previous,current in zip(calls,calls[1:]))


@pytest.mark.parametrize('changed',['etag','total','offset'])
def test_changed_entity_is_not_merged(changed):
    calls=[]
    def respond(request):
        payload=json.loads(request.content);start,end=map(int,payload['headers']['range'][6:].split('-'));end=min(end,524287)
        calls.append(start);etag='"stable"';total=524288
        if len(calls)>1:
            if changed=='etag':etag='"changed"'
            if changed=='total':total=1048576
            if changed=='offset':start+=1
        return httpx.Response(206,headers={'Content-Range':f'bytes {start}-{end}/{total}','ETag':etag},stream=httpx.ByteStream(b'x'*(end-start+1)))
    with relay_client(respond) as client:
        with pytest.raises(httpx.ReadError):client.get('https://dl1.aliyundrive.cloud/video')


def test_interrupted_segment_resumes_exact_offset():
    data=b'a'*32768+b'b'*(1048576-32768);calls=[]
    class Dropped(httpx.SyncByteStream):
        def __iter__(self):yield data[:32768];raise httpx.ReadError('lost connection')
    def respond(request):
        payload=json.loads(request.content);start,end=map(int,payload['headers']['range'][6:].split('-'));end=min(end,len(data)-1)
        calls.append(start)
        return httpx.Response(206,headers={'Content-Range':f'bytes {start}-{end}/{len(data)}','ETag':'"stable"'},
                              stream=Dropped() if len(calls)==1 else httpx.ByteStream(data[start:end+1]))
    with relay_client(respond) as client:
        assert client.get('https://dl1.aliyundrive.cloud/video').content==data
    assert calls==[0,32768]


def test_explicit_range_keeps_original_coordinates():
    def respond(request):
        payload=json.loads(request.content);assert payload['headers']['range']=='bytes=500-999'
        return httpx.Response(206,headers={'Content-Range':'bytes 500-999/10000','ETag':'"stable"'},stream=httpx.ByteStream(b'x'*500))
    with relay_client(respond) as client:
        response=client.get('https://dl1.aliyundrive.cloud/video',headers={'Range':'bytes=500-999'})
        assert response.status_code==206 and response.headers['Content-Range']=='bytes 500-999/10000' and len(response.content)==500


def test_gateway_uses_optional_relay_and_counts_original_bytes(monkeypatch):
    from aitags import media
    data=bytes(range(256))*2048
    def respond(request):
        payload=json.loads(request.content);start,end=map(int,payload['headers']['range'][6:].split('-'));end=min(end,len(data)-1)
        return httpx.Response(206,headers={'Content-Range':f'bytes {start}-{end}/{len(data)}','ETag':'"stable"'},stream=httpx.ByteStream(data[start:end+1]))
    monkeypatch.setattr(media.config,'MEDIA_FETCH_RELAY_URL','https://relay.test/media-fetch')
    monkeypatch.setattr(media.config,'MEDIA_FETCH_RELAY_TOKEN','private-owner-token')
    monkeypatch.setattr(media.config,'MEDIA_FETCH_RELAY_HOSTS',['dl*.aliyundrive.cloud'])
    monkeypatch.setattr(media,'approve_url',lambda *args:None)
    monkeypatch.setattr(media,'MediaRelayTransport',lambda *args:MediaRelayTransport(*args,transport=httpx.MockTransport(respond)))
    with media.gateway('https://dl1.aliyundrive.cloud/video','http://openlist.test',len(data),10) as (url,used):
        with httpx.Client(trust_env=False) as client:response=client.get(url)
        assert response.content==data and used[0]==len(data)


def test_permanent_denial_is_not_retried_or_leaked():
    calls=[]
    def respond(request):calls.append(1);return httpx.Response(403,stream=httpx.ByteStream(b'permission denied'))
    with relay_client(respond) as client:
        response=client.get('https://dl1.aliyundrive.cloud/video')
        assert response.status_code==403 and response.content==b'permission denied'
    assert len(calls)==1


def test_small_file_cache_reuses_one_verified_logical_stream(monkeypatch):
    from aitags import media
    from pathlib import Path
    data=b'v'*1048576;calls=[]
    def respond(request):
        payload=json.loads(request.content);start,end=map(int,payload['headers']['range'][6:].split('-'));end=min(end,len(data)-1)
        calls.append((start,end))
        return httpx.Response(206,headers={'Content-Range':f'bytes {start}-{end}/{len(data)}','ETag':'"stable"'},stream=httpx.ByteStream(data[start:end+1]))
    monkeypatch.setattr(media.config,'MEDIA_FETCH_RELAY_URL','https://relay.test/media-fetch')
    monkeypatch.setattr(media.config,'MEDIA_FETCH_RELAY_TOKEN','private-owner-token')
    monkeypatch.setattr(media.config,'MEDIA_FETCH_RELAY_HOSTS',['dl*.aliyundrive.cloud'])
    monkeypatch.setattr(media,'approve_url',lambda *args:None)
    monkeypatch.setattr(media,'MediaRelayTransport',lambda *args:MediaRelayTransport(*args,transport=httpx.MockTransport(respond)))
    with media.prepared_video_source('https://dl1.aliyundrive.cloud/video','http://openlist.test',{'max_network_bytes':len(data)},10,lambda:False,None,len(data)) as (source,used,local):
        assert local and Path(source).read_bytes()==data and used[0]==len(data)
    assert calls==[(0,262143),(262144,1048575)]


def test_midstream_relay_failure_closes_rejected_response():
    responses=[]
    def respond(request):
        response=httpx.Response(206,headers={'Content-Range':'bytes 0-262143/524288','ETag':'"stable"'},stream=httpx.ByteStream(b'x'*262144)) if not responses else httpx.Response(503,stream=httpx.ByteStream(b'unavailable'))
        responses.append(response);return response
    with relay_client(respond) as client:
        with pytest.raises(httpx.ReadError):client.get('https://dl1.aliyundrive.cloud/video')
    assert len(responses)==3 and all(response.is_closed for response in responses)


def test_transient_upstream_failure_is_retried_once_at_the_same_offset():
    calls=[]
    def respond(request):
        p=json.loads(request.content);calls.append(p['headers']['range'])
        if len(calls)==1:return httpx.Response(502,stream=httpx.ByteStream(b'upstream connection failed'))
        return httpx.Response(206,headers={'Content-Range':'bytes 0-3/4','ETag':'"film"'},stream=httpx.ByteStream(b'film'))
    with relay_client(respond) as client:assert client.get('https://dl1.aliyundrive.cloud/video').content==b'film'
    assert calls==['bytes=0-262143','bytes=0-262143']


@pytest.mark.parametrize('changed_entity',[False,True])
def test_expired_signed_url_renews_at_exact_offset_and_checks_identity(changed_entity):
    data=bytes(range(256))*2048;calls=[];renewals=[]
    def refresh():renewals.append(1);return 'https://dl1.aliyundrive.cloud/video?sig=new',{}
    def respond(request):
        payload=json.loads(request.content);calls.append(payload)
        start,end=map(int,payload['headers']['range'][6:].split('-'));end=min(end,len(data)-1)
        if start>0 and 'sig=new' not in payload['url']:
            return httpx.Response(403,stream=httpx.ByteStream(b'expired signed request'))
        etag='"changed"' if changed_entity and 'sig=new' in payload['url'] else '"stable"'
        return httpx.Response(206,headers={'Content-Range':f'bytes {start}-{end}/{len(data)}','ETag':etag},stream=httpx.ByteStream(data[start:end+1]))
    with relay_client(respond,refresh_source=refresh) as client:
        if changed_entity:
            with pytest.raises(httpx.ReadError,match='entity changed'):
                client.get('https://dl1.aliyundrive.cloud/video?sig=old',headers={'Cookie':'old-private-cookie'})
        else:
            response=client.get('https://dl1.aliyundrive.cloud/video?sig=old',headers={'Cookie':'old-private-cookie'})
            assert response.content==data
    assert renewals==[1]
    assert [p['headers']['range'] for p in calls]==['bytes=0-262143','bytes=262144-524287','bytes=262144-524287']
    assert 'cookie' not in calls[-1]['headers']


@pytest.mark.parametrize('target',['https://attacker.test/video','http://dl1.aliyundrive.cloud/video','https://user:pass@dl1.aliyundrive.cloud/video'])
def test_refresh_rejects_targets_outside_original_media_policy(target):
    calls=[]
    def respond(request):calls.append(1);return httpx.Response(403,stream=httpx.ByteStream(b'expired'))
    with relay_client(respond,refresh_source=lambda:(target,{})) as client:
        with pytest.raises(httpx.ReadError,match='not approved'):client.get('https://dl1.aliyundrive.cloud/video')
    assert len(calls)==1


def test_unchanged_denied_source_is_not_retried_forever():
    calls=[]
    def respond(request):calls.append(1);return httpx.Response(403,stream=httpx.ByteStream(b'permission denied'))
    with relay_client(respond,refresh_source=lambda:('https://dl1.aliyundrive.cloud/video',{})) as client:
        response=client.get('https://dl1.aliyundrive.cloud/video')
        assert response.status_code==403
    assert len(calls)==1


def test_refresh_can_remove_expired_cookie_without_changing_url():
    calls=[]
    def respond(request):
        p=json.loads(request.content);calls.append(p)
        if 'cookie' in p['headers']:return httpx.Response(403,stream=httpx.ByteStream(b'expired cookie'))
        return httpx.Response(206,headers={'Content-Range':'bytes 0-3/4','ETag':'"stable"'},stream=httpx.ByteStream(b'film'))
    with relay_client(respond,refresh_source=lambda:('https://dl1.aliyundrive.cloud/video',{})) as client:
        assert client.get('https://dl1.aliyundrive.cloud/video',headers={'Cookie':'expired-private-cookie'}).content==b'film'
    assert len(calls)==2 and 'cookie' not in calls[-1]['headers']


def test_local_source_cache_survives_expired_link_without_duplicate_bytes(monkeypatch):
    from aitags import media
    from pathlib import Path
    data=b'film'*262144;renewals=[]
    def refresh():renewals.append(1);return 'https://dl1.aliyundrive.cloud/video?sig=new',{}
    def respond(request):
        p=json.loads(request.content);start,end=map(int,p['headers']['range'][6:].split('-'));end=min(end,len(data)-1)
        if start and 'sig=new' not in p['url']:return httpx.Response(403,stream=httpx.ByteStream(b'expired'))
        return httpx.Response(206,headers={'Content-Range':f'bytes {start}-{end}/{len(data)}','ETag':'"film"'},stream=httpx.ByteStream(data[start:end+1]))
    monkeypatch.setattr(media.config,'MEDIA_FETCH_RELAY_URL','https://relay.test/media-fetch')
    monkeypatch.setattr(media.config,'MEDIA_FETCH_RELAY_TOKEN','private-owner-token')
    monkeypatch.setattr(media.config,'MEDIA_FETCH_RELAY_HOSTS',['dl*.aliyundrive.cloud'])
    monkeypatch.setattr(media,'approve_url',lambda *args:None)
    monkeypatch.setattr(media,'MediaRelayTransport',lambda *args,**kwargs:MediaRelayTransport(*args,**kwargs,transport=httpx.MockTransport(respond)))
    with media.prepared_video_source('https://dl1.aliyundrive.cloud/video?sig=old','http://openlist.test',{'max_network_bytes':len(data)},10,lambda:False,None,len(data),refresh) as (source,used,local):
        assert local and Path(source).read_bytes()==data
        assert used[0]==len(data)
    assert renewals==[1]
