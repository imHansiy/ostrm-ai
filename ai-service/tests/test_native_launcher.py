"""Protect same-domain native routing and the host's restricted port budget."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest


def prepare(tmp_path, **overrides):
    script=tmp_path/'deploy/freebsd/launch.py'
    script.parent.mkdir(parents=True)
    shutil.copy2(Path(__file__).resolve().parents[2]/'deploy/freebsd/launch.py',script)
    env={**os.environ,'OPENLIST_URL':'http://127.0.0.1:5244','JWT_SECRET':'fixture-only-jwt',
         'AI_BRIDGE_KEY':'fixture-only-bridge','NATIVE_APP_PREFIX':'/ostrm/',
         'NATIVE_GATEWAY_HOST':'127.0.0.1','NATIVE_JAVA_HOST':'127.0.0.1','NATIVE_AI_HOST':'127.0.0.1',**overrides}
    return subprocess.run([sys.executable,str(script),'prepare'],env=env,capture_output=True,text=True)


def test_alias_stays_inside_prefix_and_keeps_openlist_root(tmp_path):
    result=prepare(tmp_path)
    assert result.returncode==0,result.stderr
    conf=(tmp_path/'runtime/nginx.conf').read_text(encoding='utf-8')
    assert 'location /ostrm/api/' in conf and 'location /ostrm/ {' in conf
    assert 'alias ' in conf and 'try_files $uri $uri/index.html /ostrm/index.html' in conf
    assert 'absolute_redirect off' in conf
    assert 'proxy_pass "http://127.0.0.1:5244"' in conf
    assert 'data-ostrm-base="/ostrm/"' in conf
    assert 'fixture-only-bridge' not in conf and '@PREFIX@' not in conf


def test_three_endpoint_staging_uses_one_reserved_number(tmp_path):
    result=prepare(tmp_path,NATIVE_GATEWAY_HOST='::1',NATIVE_GATEWAY_PORT='43648',
                   NATIVE_AI_HOST='127.0.0.2',NATIVE_AI_PORT='43648',NATIVE_JAVA_PORT='43648')
    assert result.returncode==0,result.stderr
    conf=(tmp_path/'runtime/nginx.conf').read_text(encoding='utf-8')
    assert 'listen [::1]:43648' in conf
    assert 'http://127.0.0.2:43648' in conf and 'http://127.0.0.1:43648/api/' in conf


def test_optional_cinema_keeps_openlist_and_ai_routes_separate(tmp_path):
    result=prepare(tmp_path,CINEMA_ENABLED='true')
    assert result.returncode==0,result.stderr
    conf=(tmp_path/'runtime/nginx.conf').read_text(encoding='utf-8')
    assert 'location ~ ^/cinema(?:/|$)' in conf
    assert 'proxy_pass http://[::1]:43648' in conf
    assert 'location /ostrm/api/' in conf and 'location /ai/' in conf
    assert '@CINEMA_LOCATION@' not in conf


def test_video_mount_preserves_old_links_and_queries(tmp_path):
    result=prepare(tmp_path,CINEMA_ENABLED='true',CINEMA_BASE_PATH='/video')
    assert result.returncode==0,result.stderr
    conf=(tmp_path/'runtime/nginx.conf').read_text(encoding='utf-8')
    assert 'location ~ ^/video(?:/|$)' in conf
    assert 'location = /cinema { return 302 /video$is_args$args; }' in conf
    assert 'location ~ ^/cinema(/.*)$ { return 302 /video$1$is_args$args; }' in conf
    assert prepare(tmp_path/'invalid',CINEMA_ENABLED='true',CINEMA_BASE_PATH='/video;bad').returncode!=0


@pytest.mark.parametrize('options',[{'NATIVE_AI_HOST':'0.0.0.0'},{'NATIVE_APP_PREFIX':'/bad;prefix'},
                                    {'NATIVE_GATEWAY_PORT':'80'},{'OPENLIST_URL':'http://user:password@localhost:5244'}])
def test_invalid_runtime_configuration_fails_before_start(tmp_path,options):
    assert prepare(tmp_path,**options).returncode!=0
