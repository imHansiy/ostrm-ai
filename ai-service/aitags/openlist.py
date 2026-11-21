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

    def call(self, method, route, token='', body=None, timeout=None, _admin_retry=False):
        refreshable = (not _admin_retry and bool(config.OPENLIST_PASSWORD) and bool(token)
                       and token == self.token and route in ('fs/get','fs/list','fs/link','me'))
        def refresh_admin():
            with self.lock:
                self.login_time = 0
            return self.call(method, route, self.admin_token(), body, timeout, _admin_retry=True)
        # Retry reads only. Interactive four-second catalog probes keep their deadline.
        retries = 2 if route in ('fs/get','fs/list','me') and (timeout is None or timeout > 5) else 0
        for attempt in range(retries + 1):
            try:
                options={'timeout':timeout} if timeout is not None else {}
                r = self.client.request(method, self.url + '/api/' + route, headers={'Authorization': token}, json=body,**options)
                if r.status_code == 401 and refreshable:
                    return refresh_admin()
                r.raise_for_status()
                d = r.json()
                if d.get('code') == 200:
                    return d.get('data')
                code = d.get('code', 502)
                if code == 401 and refreshable:
                    return refresh_admin()
                reason = str(d.get('message','')).lower()
                if any(word in reason for word in ('object not found','file not found','no such file','文件不存在')):
                    raise HTTPException(404, 'OpenList 文件不存在或已移动')
                if code in (400,401,403,404):
                    raise HTTPException(code, f'OpenList 请求被拒绝（HTTP {code}），请检查路径权限或挂载凭据')
                if attempt == retries:
                    raise HTTPException(502, 'OpenList 上游请求失败，请检查挂载状态或稍后重试')
            except httpx.HTTPStatusError as error:
                code = error.response.status_code
                if code in (400,401,403,404):
                    raise HTTPException(code, f'OpenList 请求被拒绝（HTTP {code}），请检查路径权限或挂载凭据') from None
                if attempt == retries:
                    raise HTTPException(502, f'OpenList 上游 HTTP {code}，请稍后重试') from None
            except (httpx.HTTPError, ValueError):
                if attempt == retries:
                    raise HTTPException(502, '无法连接 OpenList，连接或读取超时') from None
            time.sleep(min(2, .5 * 2**attempt))

    def admin_token(self):
        if config.OPENLIST_LOCAL_CACHED_ADMIN:
            import ipaddress
            from urllib.parse import urlsplit
            try:
                local = ipaddress.ip_address(urlsplit(self.url).hostname).is_loopback
            except ValueError:
                local = False
            if not local:
                raise HTTPException(503,'缓存授权模式仅允许回环地址')
            user = self.me('')
            if user.get('role') != 2:
                raise HTTPException(503,'OpenList 未授予本地缓存管理员权限')
            return ''
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

    def get_media(self, path, timeout=None):
        """Worker-only resolution. Driver cookies must never enter browser metadata."""
        token = self.admin_token()
        obj = self.get(path, token, timeout=timeout)
        if obj.get('provider') not in config.MEDIA_DIRECT_DRIVERS:
            return obj
        link = self.call('POST', 'fs/link', token, {'path': clean_path(path)}, timeout=timeout)
        if not link or not link.get('url'):
            raise HTTPException(502, 'OpenList 未返回可读取的媒体地址')
        headers = {}
        for key, values in (link.get('header') or {}).items():
            if key.lower() not in ('cookie','authorization','user-agent','referer'):
                continue
            value = values[0] if isinstance(values, list) and values else values
            if isinstance(value, str) and len(value) <= 32768 and '\r' not in value and '\n' not in value:
                headers[key] = value
        return {**obj, 'raw_url': link['url'], '_media_headers': headers}

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
