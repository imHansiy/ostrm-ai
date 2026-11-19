"""Authenticated loopback CONNECT proxy; official API TLS stays end-to-end."""
import base64,hmac,json,socket,socketserver,threading,select,time,re
from pathlib import Path
import websocket

CONFIG=json.loads(Path(__file__).with_name('bridge.private.json').read_text())
TARGETS={'openapi.alipan.com','drive.quark.cn','pc-api.uc.cn','dl1.aliyundrive.cloud','dl-pc-zb.drive.quark.cn'}
SLOTS=threading.BoundedSemaphore(16)

class Proxy(socketserver.BaseRequestHandler):
    def handle(self):
        if not SLOTS.acquire(blocking=False):
            self.request.sendall(b'HTTP/1.1 503 Busy\r\n\r\n');return
        try: self.serve()
        except Exception as error:
            # Do not log credentials, paths, headers or vendor response payloads.
            print('bridge_error',type(error).__name__,flush=True)
        finally: SLOTS.release()

    def serve(self):
        client=self.request;client.settimeout(20)
        header=b''
        while b'\r\n\r\n' not in header:
            chunk=client.recv(4096)
            if not chunk:return
            header+=chunk
            if len(header)>16384:return
        raw,tail=header.split(b'\r\n\r\n',1)
        lines=raw.decode('iso-8859-1').split('\r\n')
        fields=dict(line.split(':',1) for line in lines[1:] if ':' in line)
        fields={k.lower():v.strip() for k,v in fields.items()}
        expected='Basic '+base64.b64encode(('openlist:'+CONFIG['proxy_secret']).encode()).decode()
        if not hmac.compare_digest(fields.get('proxy-authorization',''),expected):
            client.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="openlist"\r\n\r\n');return
        method,target,_=lines[0].split(' ',2)
        host,port=target.rsplit(':',1);host=host.strip('[]').lower();port=int(port)
        if method!='CONNECT' or port!=443 or not host or len(host)>253:
            client.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n');return
        cloud=host in TARGETS or bool(re.fullmatch(r'dl[0-9]+\.aliyundrive\.cloud|dl-pc-[a-z0-9-]+\.drive\.quark\.cn|dl-pc-[a-z0-9-]+\.pds\.uc\.cn',host))
        upstream=None
        try:
            if cloud:
                upstream=websocket.create_connection(CONFIG['url']+'/connect?host='+host+'&flow=ack',
                    header={'Authorization':'Bearer '+CONFIG['secret']},timeout=30,
                    http_no_proxy=['*'],enable_multithread=True)
            else: upstream=socket.create_connection((host,port),timeout=20)
        except Exception:
            client.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n');raise
        print('connect',host,'cloud' if cloud else 'direct',flush=True)
        client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        # Downloads are unidirectional after the HTTP request. A receive timeout
        # here would kill an actively downloading video after 90 seconds.
        client.settimeout(None)
        upstream.settimeout(90)
        stop=threading.Event()
        def finish():
            stop.set()
            try: client.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            try:upstream.close()
            except Exception:pass
        def upload():
            try:
                if tail:
                    if cloud:upstream.send_binary(tail)
                    else:upstream.sendall(tail)
                while not stop.is_set():
                    data=client.recv(32768)
                    if not data:break
                    if cloud:upstream.send_binary(data)
                    else:upstream.sendall(data)
            except Exception:pass
            finally:finish()
        thread=threading.Thread(target=upload,daemon=True);thread.start()
        try:
            while not stop.is_set():
                data=upstream.recv() if cloud else upstream.recv(32768)
                if not data:break
                if not isinstance(data,bytes):break
                client.sendall(data)
                if cloud:upstream.send('ack')
        except Exception:pass
        finally:finish();thread.join(timeout=2)

class Server(socketserver.ThreadingTCPServer):
    address_family=socket.AF_INET6
    allow_reuse_address=True
    daemon_threads=True
    request_queue_size=32
    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
        super().server_bind()

if __name__=='__main__':
    with Server(('::1',43647),Proxy) as server:
        print('bridge_ready',flush=True)
        server.serve_forever(poll_interval=1)
