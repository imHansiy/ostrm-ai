"""Optional private HTTP range relay; validates every segment before concatenation."""
import fnmatch
import re
import threading
import time
import httpx


def interval(response):
    match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
    if response.status_code!=206 or not match:
        raise httpx.ReadError('Media relay did not return a byte range')
    start,end,total=map(int,match.groups())
    if not 0<=start<=end<total or response.headers.get('Content-Encoding','identity')!='identity':
        raise httpx.ReadError('Media relay returned inconsistent range metadata')
    return start,end,total


class RelayStream(httpx.SyncByteStream):
    def __init__(self,owner,request,first,start,end,total):
        self.owner,self.request,self.response=owner,request,first
        self.position,self.end,self.total=start,end,total
        self.etag=first.headers.get('ETag','')
        self.closed=threading.Event()

    def __iter__(self):
        try:
            failures=0
            while self.position<=self.end:
                if self.closed.is_set() or time.monotonic()>=self.owner.deadline:
                    raise httpx.ReadError('Media relay transfer stopped')
                if self.response is None:
                    stop=min(self.end,self.position+self.owner.chunk_size-1)
                    try:
                        response=self.owner.open_range(self.request,self.position,stop)
                        try:
                            a,b,total=interval(response)
                            if a!=self.position or b>stop or total!=self.total or response.headers.get('ETag')!=self.etag:
                                raise httpx.ReadError('Media relay entity changed')
                        except BaseException:
                            response.close();raise
                        self.response=response
                    except (httpx.ConnectError,httpx.ConnectTimeout,httpx.ReadTimeout):
                        failures+=1
                        if failures>3:raise
                        if self.closed.wait(min(2, .5*failures)):raise httpx.ReadError('Media relay transfer stopped')
                        continue
                _,part_end,_=interval(self.response)
                try:
                    for chunk in self.response.iter_raw(32768):
                        if self.closed.is_set() or time.monotonic()>=self.owner.deadline:
                            raise httpx.ReadError('Media relay transfer stopped')
                        if self.position+len(chunk)>part_end+1:
                            raise httpx.ReadError('Media relay response exceeded declared range')
                        self.position+=len(chunk)
                        yield chunk
                    if self.position!=part_end+1:raise httpx.ReadError('Media relay response was incomplete')
                    failures=0
                except (httpx.ReadError,httpx.ReadTimeout,httpx.RemoteProtocolError):
                    failures+=1
                    if failures>3 or self.closed.is_set():raise
                finally:
                    if self.response is not None:self.response.close()
                    self.response=None
        finally:self.close()

    def close(self):
        self.closed.set()
        if self.response is not None:self.response.close()


class MediaRelayTransport(httpx.BaseTransport):
    def __init__(self,endpoint,token,hosts,deadline,*,transport=None,chunk_size=4*1024**2,refresh_source=None):
        self.endpoint,self.token,self.hosts,self.deadline=endpoint,token,hosts,deadline
        self.chunk_size=max(32768,min(8*1024**2,chunk_size))
        self.client=httpx.Client(transport=transport,timeout=15,trust_env=False,follow_redirects=False)
        self.direct=httpx.HTTPTransport()
        self.refresh_source=refresh_source
        self.source_url=None
        self.source_headers=None
        self.refresh_lock=threading.Lock()

    def open_range(self,request,start,end):
        allowed={'range','cookie','authorization','user-agent','referer','if-match'}
        renewed=False
        transient_retried=False
        for attempt in range(4):
            if time.monotonic()>=self.deadline:raise httpx.ReadTimeout('Media relay deadline exceeded')
            headers={key:value for key,value in request.headers.items() if key.lower() in allowed}
            if self.source_headers is not None:
                for key in ('cookie','authorization','user-agent','referer'):headers.pop(key,None)
                headers.update({k.lower():v for k,v in self.source_headers.items() if k.lower() in allowed})
            headers['range']=f'bytes={start}-{end}'
            target=self.source_url or str(request.url)
            response=self.client.send(self.client.build_request('POST',self.endpoint,
                headers={'Authorization':'Bearer '+self.token,'Accept-Encoding':'identity'},
                json={'url':target,'headers':headers}),stream=True)
            if response.status_code in (502,503,504) and not transient_retried and attempt<3:
                response.close();transient_retried=True
                time.sleep(.5)
                continue
            if response.status_code!=403:return response
            body=bytearray()
            for chunk in response.iter_raw(4096):
                body.extend(chunk[:65536-len(body)])
                if len(body)>=65536:break
            temporary=b'RequestDeniedByCallback' in body and b'ExceedMaxConcurrency' in body
            if not temporary:
                if self.refresh_source and not renewed and attempt<3:
                    response.close()
                    with self.refresh_lock:
                        if self.source_url and self.source_url!=target:
                            changed=True
                        else:
                            url,fresh_headers=self.refresh_source()
                            parsed=httpx.URL(url)
                            if parsed.scheme!='https' or parsed.userinfo or not any(fnmatch.fnmatchcase(parsed.host,h) for h in self.hosts):
                                raise httpx.ReadError('Refreshed media target is not approved')
                            auth={'cookie','authorization','referer'}
                            if any(k.lower()=='user-agent' for k in fresh_headers):auth.add('user-agent')
                            previous=self.source_headers if self.source_headers is not None else dict(request.headers)
                            old_auth={k.lower():v for k,v in previous.items() if k.lower() in auth}
                            new_auth={k.lower():v for k,v in fresh_headers.items() if k.lower() in auth}
                            changed=str(parsed)!=target or new_auth!=old_auth
                            if changed:self.source_url,self.source_headers=str(parsed),dict(fresh_headers)
                    renewed=True
                    if changed:continue
                response.close()
                return httpx.Response(403,stream=httpx.ByteStream(bytes(body)),headers=response.headers,request=request)
            response.close()
            if attempt==3:raise httpx.ReadError('Media relay provider capacity temporarily unavailable')
            time.sleep(.5*2**attempt)

    def handle_request(self,request):
        if request.method not in ('GET','HEAD') or request.url.scheme!='https' or not any(fnmatch.fnmatchcase(request.url.host,host) for host in self.hosts):
            return self.direct.handle_request(request)
        match=re.fullmatch(r'bytes=(\d+)-(\d*)',request.headers.get('Range',''))
        if request.headers.get('Range') and not match:return httpx.Response(416,request=request)
        start=int(match[1]) if match else 0
        requested_end=int(match[2]) if match and match[2] else None
        if requested_end is not None and requested_end<start:return httpx.Response(416,request=request)
        first_end=min(start+256*1024-1,requested_end) if requested_end is not None else start+256*1024-1
        first=self.open_range(request,start,first_end)
        if first.status_code!=206:return first
        try:
            a,b,total=interval(first)
            etag=first.headers.get('ETag','')
            end=min(total-1,requested_end) if requested_end is not None else total-1
            if a!=start or b>first_end or not etag or etag.startswith('W/'):
                raise httpx.ReadError('Media relay cannot validate a stable entity')
            headers=httpx.Headers(first.headers)
            headers['Content-Length']=str(end-start+1);headers['Accept-Ranges']='bytes'
            if match:headers['Content-Range']=f'bytes {start}-{end}/{total}'
            elif 'Content-Range' in headers:del headers['Content-Range']
            stream=RelayStream(self,request,first,start,end,total)
            if request.method=='HEAD':stream.close();stream=httpx.ByteStream(b'')
            return httpx.Response(206 if match else 200,headers=headers,stream=stream,request=request)
        except BaseException:
            first.close();raise

    def close(self):
        self.client.close();self.direct.close()
