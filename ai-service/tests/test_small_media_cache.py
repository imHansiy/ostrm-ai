from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import pytest
from aitags import media


def test_small_callback_video_is_fetched_once_and_cleaned(tmp_path, monkeypatch):
    data = b'video-bytes' * 10000
    calls = []
    class Origin(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            calls.append(self.headers.get('Range'))
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setattr(media, '_ali_callback', lambda _: True)
    settings = {'max_network_bytes': len(data)*2}
    try:
        with media.prepared_video_source(origin+'/video', origin, settings, 10, lambda:False, None, len(data)) as (source, used, local):
            assert local and Path(source).read_bytes() == data
            assert used[0] == len(data)
            assert calls == [None]
        assert not Path(source).exists()
        with pytest.raises(media.MediaError, match='大小变化'):
            with media.prepared_video_source(origin+'/video', origin, settings, 10, lambda:False, None, len(data)-1):
                pytest.fail('changed-size video must not reach inference')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.mark.parametrize('callback,size,limit', [(False,10,20),(True,30,20),(True,0,20),(True,10,0)])
def test_other_providers_large_or_unknown_files_keep_streaming(monkeypatch,callback,size,limit):
    from contextlib import contextmanager
    @contextmanager
    def gateway(*args, **kwargs): yield 'stream://source', [0]
    monkeypatch.setattr(media, 'gateway', gateway)
    monkeypatch.setattr(media, '_ali_callback', lambda _: callback)
    monkeypatch.setattr(media.config, 'MEDIA_SMALL_CACHE_BYTES', limit)
    with media.prepared_video_source('url','openlist',{'max_network_bytes':100},10,lambda:False,None,size) as (source,used,local):
        assert source == 'stream://source' and not local
