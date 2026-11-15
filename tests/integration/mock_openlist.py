"""Deterministic OpenList/model contract fixture; real MP4 bytes and HTTP Range.

The model is a fixture, not an accuracy benchmark or a live MiniCPM call.
"""
import base64
import json
import os
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PUBLIC=os.getenv('MOCK_PUBLIC_URL','http://127.0.0.1:18789')
directory=tempfile.TemporaryDirectory();clip=Path(directory.name)/'blue.mp4'
subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=320x180:r=12',
    '-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','3','-c:v','libx264',
    '-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',str(clip)],check=True)
video=clip.read_bytes();stats={'video_requests':0,'model_requests':0,'model_video_bytes':0}
obj={'name':'blue.2026.mp4','is_dir':False,'size':len(video),'modified':'2026-11-01T00:00:00Z',
     'hash_info':{},'hashinfo':'','raw_url':PUBLIC+'/raw/blue.mp4'}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def json(self,data,status=200):
        payload=json.dumps(data,ensure_ascii=False).encode();self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)))
        self.end_headers();self.wfile.write(payload)
    def do_HEAD(self):self.do_GET()
    def do_GET(self):
        if self.path.startswith('/raw/'):
            stats['video_requests']+=1;start,end=0,len(video)-1
            value=self.headers.get('Range','')
            if value.startswith('bytes='):
                left,right=value[6:].split('-',1);start=int(left or 0);end=min(int(right or end),end)
            if start>=len(video):self.send_response(416);self.end_headers();return
            self.send_response(206 if value else 200)
            self.send_header('Content-Type','video/mp4');self.send_header('Accept-Ranges','bytes')
            self.send_header('Content-Length',str(end-start+1))
            if value:self.send_header('Content-Range',f'bytes {start}-{end}/{len(video)}')
            self.end_headers()
            if self.command!='HEAD':self.wfile.write(video[start:end+1])
        elif self.path=='/test/stats':self.json(stats)
        elif self.path.startswith('/api/me'):
            token=self.headers.get('Authorization','')
            if token=='fixture-admin':self.json({'code':200,'data':{'username':'protocol-test','role':2,'base_path':'/'}})
            elif token=='fixture-limited':self.json({'code':200,'data':{'username':'limited','role':0,'base_path':'/videos'}})
            else:self.json({'code':401,'message':'expired'},401)
        else:self.json({'code':404,'message':'not found'},404)
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
        if self.path=='/api/auth/login':return self.json({'code':200,'data':{'token':'fixture-admin'}})
        if self.path=='/api/fs/get':
            data=obj if body.get('path')=='/videos/blue.2026.mp4' else {'name':'videos','is_dir':True}
            return self.json({'code':200,'data':data})
        if self.path=='/api/fs/list':return self.json({'code':200,'data':{'content':[obj],'total':1}})
        if self.path=='/v1/chat/completions':
            stats['model_requests']+=1
            for message in body.get('messages',[]):
                for part in message.get('content',[]) if isinstance(message.get('content'),list) else []:
                    if part.get('type')=='video_url':
                        data=base64.b64decode(part['video_url']['url'].split(',',1)[1]);assert b'ftyp' in data[:40]
                        stats['model_video_bytes']+=len(data)
            result={'summary':'蓝色画面伴随持续音调，用于验证完整视频识别与协议导出链路。','tags':['蓝色','测试视频'],'visible_text':[],'uncertainty':''}
            return self.json({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result,ensure_ascii=False)}}], 'usage':{}})
        self.json({'code':403,'message':'fixture is read-only'},403)

if __name__=='__main__':
    ThreadingHTTPServer(('0.0.0.0',int(os.getenv('MOCK_PORT','18789'))),Handler).serve_forever()
