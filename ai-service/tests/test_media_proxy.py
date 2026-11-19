from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import httpx
import pytest
from aitags import config
from aitags.media import gateway, MediaError


@contextmanager
def endpoint(payload):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_selected_media_uses_proxy_and_tracks_real_bytes(monkeypatch):
    with endpoint(b'direct') as (source, direct_seen), endpoint(b'video-content') as (proxy, proxy_seen):
        monkeypatch.setattr(config, 'MEDIA_PROXY_URL', proxy)
        monkeypatch.setattr(config, 'MEDIA_PROXY_HOSTS', ['127.0.0.1'])
        with gateway(source+'/movie.mp4', source, 100, 5) as (url, used):
            response = httpx.get(url, trust_env=False)
            assert response.content == b'video-content'
            assert used[0] == len(response.content)
        assert direct_seen == []
        assert proxy_seen == [source+'/movie.mp4']


def test_unselected_media_stays_direct(monkeypatch):
    with endpoint(b'direct') as (source, direct_seen), endpoint(b'proxy') as (proxy, proxy_seen):
        monkeypatch.setattr(config, 'MEDIA_PROXY_URL', proxy)
        monkeypatch.setattr(config, 'MEDIA_PROXY_HOSTS', ['dl1.aliyundrive.cloud'])
        with gateway(source+'/movie.mp4', source, 100, 5) as (url, used):
            assert httpx.get(url, trust_env=False).content == b'direct'
        assert direct_seen == ['/movie.mp4']
        assert proxy_seen == []


def test_proxy_does_not_bypass_media_allowlist(monkeypatch):
    monkeypatch.setattr(config, 'MEDIA_HOSTS', [])
    monkeypatch.setattr(config, 'MEDIA_PROXY_URL', 'http://127.0.0.1:9999')
    monkeypatch.setattr(config, 'MEDIA_PROXY_HOSTS', ['*'])
    with pytest.raises(MediaError, match='MEDIA_ALLOWED_HOSTS'):
        with gateway('https://unapproved.example/movie.mp4', 'http://openlist:5244', 100, 5):
            pass
