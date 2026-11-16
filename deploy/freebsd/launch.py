"""Native FreeBSD launcher: Java + AI + nginx, configured without shell-sourcing secrets."""
import json
import os
from pathlib import Path
import shutil
import sys
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[2]
os.chdir(ROOT)
load_dotenv(ROOT/'.env',override=False)
defaults={
    'AI_DATA_DIR':ROOT/'data/ai','MEDIA_EXPORT_DIR':ROOT/'strm',
    'APP_DATA_PATH':ROOT/'data','APP_CONFIG_PATH':ROOT/'data/config',
    'APP_DATABASE_PATH':ROOT/'data/db/ostrm.sqlite3','APP_USER_INFO_PATH':ROOT/'data/config/userInfo.json',
    'APP_LOG_PATH':ROOT/'logs/java','APP_FRONTEND_LOGS_PATH':ROOT/'logs/frontend',
    'APP_STRM_PATH':ROOT/'strm','AI_SERVICE_URL':'http://'+os.getenv('NATIVE_AI_HOST','127.0.0.1')+':'+os.getenv('NATIVE_AI_PORT','43649'),
    'AI_PROXY_ENABLED':'false','GITHUB_REPO_OWNER':'imHansiy','GITHUB_REPO_NAME':'ostrm-ai',
}
for key,value in defaults.items():os.environ.setdefault(key,str(value))
mode=sys.argv[1] if len(sys.argv)>1 else 'prepare'

if mode=='prepare':
    for name in ('runtime','runtime/client_temp','runtime/proxy_temp','data/config','data/db','data/ai','strm','logs/java','logs/frontend'):
        (ROOT/name).mkdir(parents=True,exist_ok=True,mode=0o700)
    if not os.environ.get('AI_BRIDGE_KEY') or not os.environ.get('JWT_SECRET'):
        raise SystemExit('Configure independent AI_BRIDGE_KEY and JWT_SECRET in .env')
    prefix=os.getenv('NATIVE_APP_PREFIX','/ostrm/').strip('/')
    if not prefix or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in prefix):
        raise SystemExit('NATIVE_APP_PREFIX must be one local URL segment')
    prefix='/'+prefix+'/'
    ports={name:int(os.getenv(key,default)) for name,key,default in (
        ('gateway','NATIVE_GATEWAY_PORT','43647'),('java','NATIVE_JAVA_PORT','43648'),('ai','NATIVE_AI_PORT','43649'))}
    if any(not 1024<=port<=65535 for port in ports.values()):raise SystemExit('Invalid native port')
    import ipaddress
    hosts={name:os.getenv('NATIVE_'+name.upper()+'_HOST','127.0.0.1') for name in ('gateway','java','ai')}
    if any(not ipaddress.ip_address(host).is_loopback for host in hosts.values()):raise SystemExit('Native hosts must be loopback addresses')
    openlist=os.environ['OPENLIST_URL'].rstrip('/')
    from urllib.parse import urlsplit
    target=urlsplit(openlist)
    if target.scheme not in ('http','https') or not target.hostname or target.username or target.password or target.path not in ('','/') or target.query or target.fragment:
        raise SystemExit('OPENLIST_URL must be an HTTP(S) origin without credentials')
    def q(value):return json.dumps(str(value),ensure_ascii=False).replace('$','\\$')
    source='''daemon off;
master_process off;
worker_processes 1;
pid @ROOT@/runtime/nginx.pid;
error_log @ROOT@/runtime/nginx-error.log warn;
events { worker_connections 256; }
http {
    types {
        text/html html;
        text/css css;
        application/javascript js mjs;
        application/json json;
        image/svg+xml svg;
        image/png png;
        image/jpeg jpg jpeg;
        image/webp webp;
        image/x-icon ico;
        font/woff2 woff2;
    }
    default_type application/octet-stream;
    access_log off;
    client_body_temp_path @ROOT@/runtime/client_temp;
    proxy_temp_path @ROOT@/runtime/proxy_temp;
    map $http_upgrade $upgrade_connection { default upgrade; '' close; }
    server {
        listen @GATEWAY_HOST@:@GATEWAY@;
        server_name _;
        absolute_redirect off;
        @CINEMA_LOCATION@
        client_max_body_size 0;
        if ($arg_ai = video-tags) { return 302 @PREFIX@ai-center?tab=$arg_view; }
        location = @PREFIX_NO_SLASH@ { return 302 @PREFIX@; }
        location @PREFIX@api/ {
            proxy_pass http://@JAVA_HOST@:@JAVA@/api/;
            proxy_set_header Host $host;
            proxy_read_timeout 210s;
            client_max_body_size 1m;
            add_header Cache-Control "no-store" always;
        }
        location @PREFIX@ {
            alias @PUBLIC@;
            try_files $uri $uri/index.html @PREFIX@index.html;
            add_header Cache-Control "no-store" always;
        }
        location = /ai/exports { return 302 @PREFIX@ai-center?tab=publications; }
        location = /ai/assets/exports.html { return 302 @PREFIX@ai-center?tab=publications; }
        location = /ai/cinema { return 302 @PREFIX@ai-center?tab=library; }
        location = /ai/cinema/ { return 302 @PREFIX@ai-center?tab=library; }
        location /ai/ {
            proxy_pass http://@AI_HOST@:@AI@;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
            proxy_read_timeout 1900s;
            client_max_body_size 64k;
            add_header Cache-Control "no-store" always;
        }
        location /media/ {
            proxy_pass http://@AI_HOST@:@AI@;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
            proxy_read_timeout 210s;
            client_max_body_size 64k;
            add_header Cache-Control "no-store" always;
        }
        location / {
            proxy_pass @OPENLIST@;
            proxy_set_header Accept-Encoding "";
            sub_filter '<meta name="generator" content="OpenList" >' '<meta name="generator" content="OpenList" ><script defer data-ostrm-base="@PREFIX@" src="/ai/assets/ai.js"></script>';
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
            proxy_set_header X-Forwarded-For $http_x_forwarded_for;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $upgrade_connection;
            proxy_read_timeout 3600s;
            proxy_request_buffering off;
            proxy_buffering off;
        }
    }
}
'''
    # The root path appears as a directive argument and as a path prefix: quote
    # complete paths, never interpolate environment values as nginx directives.
    for suffix in ('/runtime/nginx.pid','/runtime/nginx-error.log','/runtime/client_temp','/runtime/proxy_temp'):
        source=source.replace('@ROOT@'+suffix,q(str(ROOT)+suffix))
    values={'@PUBLIC@':q(str(ROOT/'public')+'/'),'@OPENLIST@':q(openlist),'@PREFIX_NO_SLASH@':prefix.rstrip('/'),'@PREFIX@':prefix,
            '@GATEWAY@':str(ports['gateway']),'@JAVA@':str(ports['java']),'@AI@':str(ports['ai'])}
    for key,value in values.items():source=source.replace(key,value)
    for name,host in hosts.items():source=source.replace('@'+name.upper()+'_HOST@','['+host+']' if ':' in host else host)
    cinema=''
    if os.getenv('CINEMA_ENABLED','false').lower() in ('1','true'):
        cinema='''location ~ ^/cinema(?:/|$) {
            proxy_pass http://[::1]:43648;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
            proxy_set_header X-Forwarded-For $http_x_forwarded_for;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_read_timeout 120s;
            proxy_buffering off;
            client_max_body_size 1m;
        }'''
    source=source.replace('@CINEMA_LOCATION@',cinema)
    (ROOT/'runtime/nginx.conf').write_text(source,encoding='utf-8')
    print('Native runtime paths and gateway configuration prepared.')
elif mode=='cinema-start':
    if os.getenv('CINEMA_ENABLED','false').lower() in ('1','true'):
        import subprocess
        subprocess.run(['/bin/sh',str(ROOT.parent/'libretv/deploy/freebsd/service.sh'),'start'],check=True)
elif mode=='java':
    binary=os.getenv('JAVA_BIN') or shutil.which('java21') or ('/usr/local/bin/java21' if Path('/usr/local/bin/java21').is_file() else shutil.which('java'))
    if not binary:raise SystemExit('Java 21 is required')
    args=[binary,'-Xms32m','-Xmx192m','-XX:MaxMetaspaceSize=160m','-XX:ReservedCodeCacheSize=64m',
          '-XX:ActiveProcessorCount=2','-Xss512k','-Djava.awt.headless=true','-Dfile.encoding=UTF-8',
          '-jar',str(ROOT/'openlisttostrm.jar'),'--server.address='+os.getenv('NATIVE_JAVA_HOST','127.0.0.1'),'--server.port='+os.getenv('NATIVE_JAVA_PORT','43648')]
    os.execve(binary,args,os.environ)
elif mode=='ai':
    args=[sys.executable,'-m','uvicorn','aitags.app:app','--host',os.getenv('NATIVE_AI_HOST','127.0.0.1'),'--port',os.getenv('NATIVE_AI_PORT','43649'),
          '--workers','1','--no-access-log','--timeout-graceful-shutdown','30']
    os.execve(sys.executable,args,os.environ)
elif mode=='gateway':
    binary=shutil.which('nginx') or '/usr/local/sbin/nginx'
    os.execve(binary,[binary,'-e',str(ROOT/'runtime/nginx-error.log'),'-p',str(ROOT)+'/', '-c',str(ROOT/'runtime/nginx.conf')],os.environ)
else:raise SystemExit('Usage: launch.py prepare|java|ai|gateway')
