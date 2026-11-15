import hashlib
import posixpath
import threading
import time
import httpx
from urllib.parse import quote
from fastapi import HTTPException
from . import config


def clean_path(path):
    if not isinstance(path, str) or not path.startswith('/') or '\\' in path or '\x00' in path or any(x == '..' for x in path.split('/')):
        raise HTTPException(400, '非法路径')
    return posixpath.normpath(path)


def canonical(user, path):
    return clean_path(posixpath.join(clean_path(user.get('base_path') or '/'), clean_path(path).lstrip('/')))


def relative(user, path):
    base = clean_path(user.get('base_path') or '/')
    if base == '/':
        return path
    if path == base:
        return '/'
    return '/' + path[len(base) + 1:] if path.startswith(base + '/') else None


class OpenList:
    def __init__(self, url=None):
        self.url = (url or config.OPENLIST_URL).rstrip('/')
        self.client = httpx.Client(timeout=30, follow_redirects=False, trust_env=True)
        self.token = config.OPENLIST_TOKEN
        self.lock = threading.Lock()
        self.login_time = 0

    def call(self, method, route, token='', body=None, timeout=None):
        try:
            options={'timeout':timeout} if timeout is not None else {}
            r = self.client.request(method, self.url + '/api/' + route, headers={'Authorization': token}, json=body,**options)
            r.raise_for_status()
            d = r.json()
            if d.get('code') != 200:
                code = d.get('code', 502)
                raise HTTPException(code if code in (400,401,403,404) else 502, 'OpenList 拒绝访问或请求失败')
            return d.get('data')
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, '无法连接 OpenList') from None

    def admin_token(self):
        with self.lock:
            if config.OPENLIST_PASSWORD and (not self.token or time.time() - self.login_time > 3600):
                pwd = hashlib.sha256((config.OPENLIST_PASSWORD + '-https://github.com/alist-org/alist').encode()).hexdigest()
                self.token = self.call('POST', 'auth/login/hash', body={'username': config.OPENLIST_USERNAME, 'password': pwd})['token']
                self.login_time = time.time()
            if not self.token:
                raise HTTPException(503, '服务端尚未配置 OpenList 扫描凭据')
            return self.token

    def me(self, token):
        user = self.call('GET', 'me', token)
        if user.get('disabled'):
            raise HTTPException(403, '用户已禁用')
        return user

    def get(self, path, token, password='', timeout=None):
        options={'timeout':timeout} if timeout is not None else {}
        return self.call('POST', 'fs/get', token, {'path': clean_path(path), 'password': password},**options)

    def listing(self, path, token, page=1, size=200, password='', timeout=None):
        options={'timeout':timeout} if timeout is not None else {}
        return self.call('POST', 'fs/list', token, {'path': clean_path(path), 'password': password, 'page': page, 'per_page': size, 'refresh': False},**options)

    def close(self):
        self.client.close()

    def put_cover(self, path, jpeg, token):
        try:
            response = self.client.put(self.url+'/api/fs/put',content=jpeg,headers={
                'Authorization':token,'File-Path':quote(clean_path(path),safe=''),
                'Content-Type':'image/jpeg','As-Task':'false','Overwrite':'false',
            })
            response.raise_for_status()
            if response.json().get('code') != 200:
                raise HTTPException(502,'挂载未接受封面写入')
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502,'封面写入失败') from None
