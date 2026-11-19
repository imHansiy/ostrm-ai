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
