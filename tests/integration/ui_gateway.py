"""Test-only static SPA and same-origin API gateway. Never used in production."""
import mimetypes
import os
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import httpx
ROOT=Path(__file__).resolve().parents[2]/'frontend/.output/public'
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):self.handle_request()
    def do_POST(self):self.handle_request()
    def do_PUT(self):self.handle_request()
    def handle_request(self):
        if self.path.startswith('/api/'):
            body=self.rfile.read(int(self.headers.get('Content-Length',0)))
            response=httpx.request(self.command,'http://127.0.0.1:18080'+self.path,content=body,
                headers={key:value for key,value in self.headers.items() if key.lower() not in ('host','content-length','connection')},timeout=60)
            data=response.content;status=response.status_code;mime=response.headers.get('content-type','application/json')
        else:
            target=(ROOT/urlsplit(self.path).path.lstrip('/')).resolve()
            if not target.is_relative_to(ROOT.resolve()):self.send_error(403);return
            if target.is_dir():target=target/'index.html'
            if not target.is_file():target=ROOT/'index.html'
            data=target.read_bytes();status=200;mime=mimetypes.guess_type(target)[0] or 'application/octet-stream'
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)))
        self.end_headers();self.wfile.write(data)
ThreadingHTTPServer(('127.0.0.1',18790),Handler).serve_forever()
