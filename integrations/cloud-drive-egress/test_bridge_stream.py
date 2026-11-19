import base64,importlib.util,socket,threading,time
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('egress_bridge',Path(__file__).with_name('bridge.py'))
bridge=importlib.util.module_from_spec(spec)
with patch.object(Path,'read_text',return_value='{"proxy_secret":"test-only"}'):
    spec.loader.exec_module(bridge)

def test_active_download_survives_quiet_upload_direction():
    bridge.CONFIG={'proxy_secret':'test-only'}
    client,request=socket.socketpair()
    upstream,origin=socket.socketpair()
    class AcceleratedTimeouts:
        def __getattr__(self,name):return getattr(request,name)
        def settimeout(self,value):request.settimeout(None if value is None else value/900)
    payload=b'x'*4096
    def serve_origin():
        try:
            for _ in range(30):
                origin.sendall(payload);time.sleep(.04)
        except OSError:pass
        finally:origin.close()
    auth=base64.b64encode(b'openlist:test-only').decode()
    client.sendall(('CONNECT origin.example:443 HTTP/1.1\r\nProxy-Authorization: Basic '+auth+'\r\n\r\n').encode())
    worker=threading.Thread(target=lambda:bridge.Proxy(AcceleratedTimeouts(),('::1',1),None))
    sender=threading.Thread(target=serve_origin)
    try:
        with patch.object(bridge.socket,'create_connection',return_value=upstream):
            worker.start();sender.start()
            client.settimeout(3)
            data=b''
            while True:
                chunk=client.recv(16384)
                if not chunk:break
                data+=chunk
        header,body=data.split(b'\r\n\r\n',1)
        assert b'200 Connection Established' in header
        assert body==payload*30
    finally:
        client.close();request.close();upstream.close();origin.close()
        worker.join(timeout=3);sender.join(timeout=3)


def test_model_upload_uses_ack_credit_without_truncating_large_request():
    import queue
    bridge.CONFIG={'proxy_secret':'test-only','url':'wss://relay.test','secret':'test-only'}
    client,request=socket.socketpair();payload=b'm'*2*1024**2
    class Cloud:
        def __init__(self):self.events=queue.Queue();self.received=bytearray();self.pending=0;self.maximum=0;self.lock=threading.Lock()
        def settimeout(self,_):pass
        def send_binary(self,data):
            with self.lock:
                self.received.extend(data);self.pending+=len(data);self.maximum=max(self.maximum,self.pending)
            self.events.put(('uack',len(data)))
            if len(self.received)==len(payload):self.events.put((b'model-response',0));self.events.put((b'',0))
        def send(self,data):assert data=='ack'
        def recv(self):
            item,size=self.events.get(timeout=5)
            if item=='uack':
                time.sleep(.001)
                with self.lock:self.pending-=size
            return item
        def close(self):self.events.put((b'',0))
    cloud=Cloud()
    def connect(url,**kwargs):
        assert 'host=api.modelbest.cn&flow=ack&upload=ack' in url
        return cloud
    auth=base64.b64encode(b'openlist:test-only').decode()
    client.sendall(('CONNECT api.modelbest.cn:443 HTTP/1.1\r\nProxy-Authorization: Basic '+auth+'\r\n\r\n').encode())
    worker=threading.Thread(target=lambda:bridge.Proxy(request,('::1',1),None))
    try:
        with patch.object(bridge.websocket,'create_connection',side_effect=connect):
            worker.start();client.settimeout(5)
            header=client.recv(4096);assert b'200 Connection Established' in header
            client.sendall(payload)
            response=b''
            while True:
                part=client.recv(4096)
                if not part:break
                response+=part
        assert response==b'model-response' and bytes(cloud.received)==payload
        assert cloud.maximum<=512*1024
    finally:
        client.close();request.close();worker.join(timeout=5)
