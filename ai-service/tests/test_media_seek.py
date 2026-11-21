import re,shutil,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
from aitags import media


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='requires real FFmpeg tools')
def test_whole_video_compression_seeks_verified_relay_tail_metadata(tmp_path,monkeypatch):
    import httpx,json
    from aitags.media_relay import MediaRelayTransport
    from aitags.config import DEFAULTS
    path=tmp_path/'tail-video.mp4'
    media.run_process(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=duration=12:size=960x540:rate=15',
                       '-c:v','libx264','-preset','ultrafast','-crf','18','-pix_fmt','yuv420p',str(path)],30)
    data=path.read_bytes();assert len(data)>1024**2
    calls=[]
    def upstream(request):
        body=json.loads(request.content)
        start,end=map(int,body['headers']['range'][6:].split('-'));end=min(end,len(data)-1)
        calls.append(start)
        return httpx.Response(206,headers={'Content-Range':f'bytes {start}-{end}/{len(data)}',
                              'ETag':'"unchanged-film"'},stream=httpx.ByteStream(data[start:end+1]))
    monkeypatch.setattr(media.config,'MEDIA_FETCH_RELAY_URL','https://relay.test/media-fetch')
    monkeypatch.setattr(media.config,'MEDIA_FETCH_RELAY_TOKEN','private-test-token')
    monkeypatch.setattr(media.config,'MEDIA_FETCH_RELAY_HOSTS',['dl*.aliyundrive.cloud'])
    monkeypatch.setattr(media.config,'MEDIA_SMALL_CACHE_BYTES',0)
    monkeypatch.setattr(media,'approve_url',lambda *args:None)
    monkeypatch.setattr(media,'MediaRelayTransport',lambda *args,**kwargs:MediaRelayTransport(
        *args,**kwargs,transport=httpx.MockTransport(upstream)))
    sample=media.compress_video('https://dl1.aliyundrive.cloud/tail.mp4','http://openlist.test',
                                {**DEFAULTS,'media_timeout':30},source_size=len(data))
    assert sample['duration']==pytest.approx(12,abs=.1)
    assert 0<sample['video_bytes']<=23*1024**2
    assert any(start>len(data)//2 for start in calls)
    assert sample['network_bytes']<2*len(data)


@pytest.mark.parametrize('first',[{'streams':[{}],'format':{}},media.MediaError('unsupported fast probe')])
def test_duration_probe_falls_back_without_extending_total_deadline(monkeypatch,first):
    import json
    calls=[]
    def run(args,timeout,cancelled):
        calls.append((args,timeout))
        if len(calls)==1:
            if isinstance(first,Exception):raise first
            return json.dumps(first).encode()
        return b'{"streams":[{"duration":"12.5"}]}'
    monkeypatch.setattr(media,'run_process',run)
    assert media.video_duration('local.mp4',[],60,lambda:False)==12.5
    assert '-nofind_stream_info' in calls[0][0] and '-nofind_stream_info' not in calls[1][0]
    assert calls[0][1]<=30 and calls[1][1]<=60


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='requires real FFmpeg tools')
def test_tail_metadata_probe_seeks_without_reading_whole_video(tmp_path,monkeypatch):
    # Exercise provider admission with a real slow, ranged HTTP source.
    monkeypatch.setattr(media,'_ali_callback',lambda url:True)
    path=tmp_path/'tail.mp4'
    media.run_process(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=duration=12:size=960x540:rate=15',
                       '-c:v','libx264','-preset','ultrafast','-crf','18','-pix_fmt','yuv420p',str(path)],30)
    data=path.read_bytes();assert len(data)>1024**2
    ranges=[]
    class Origin(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_GET(self):
            start=0;end=len(data)-1
            match=re.fullmatch(r'bytes=(\d+)-(\d*)',self.headers.get('Range',''))
            if match:
                start=int(match[1]);end=min(end,int(match[2])) if match[2] else end
                self.send_response(206)
                self.send_header('Content-Range',f'bytes {start}-{end}/{len(data)}')
            else:self.send_response(200)
            ranges.append(start)
            self.send_header('Content-Length',str(end-start+1));self.end_headers()
            try:
                for offset in range(start,end+1,4096):
                    self.wfile.write(data[offset:min(offset+4096,end+1)])
                    time.sleep(.002)
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Origin)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin=f'http://127.0.0.1:{server.server_port}'
    try:
        with media.gateway(origin+'/tail.mp4',origin,len(data)*2,15) as (url,used):
            duration=media.video_duration(url,['-protocol_whitelist','http,tcp'],10,lambda:False)
            assert duration==pytest.approx(12,abs=.1)
            assert any(start>len(data)//2 for start in ranges)
            assert used[0]<len(data)
    finally:server.shutdown();server.server_close();thread.join(timeout=2)
