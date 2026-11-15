import hashlib
import posixpath
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from fastapi import HTTPException

from . import config
from .media import FORMATS, MediaError, gateway, run_process, video_duration

COVER_MAX_BYTES = 96 * 1024
CACHE_MAX_BYTES = 64 * 1024**2


def cover_path(path, fingerprint):
    parent, name = posixpath.split(path)
    version = hashlib.sha256((path + '\0' + fingerprint).encode()).hexdigest()[:16]
    suffix = f'.aitags-cover-{version}.jpg'
    if len((name+suffix).encode()) > 240:
        name = 'video'
    return posixpath.join(parent, name+suffix)


def extract_cover(url, openlist_url, cancelled):
    options = ['-protocol_whitelist','http,tcp','-format_whitelist',FORMATS,'-rw_timeout','7000000']
    with gateway(url,openlist_url,16*1024**2,25,cancelled) as (source,_):
        duration = video_duration(source,options,8,cancelled)
        with TemporaryDirectory(prefix='aitags-cover-') as directory:
            output = Path(directory)/'cover.jpg'
            run_process([config.FFMPEG,'-v','error','-nostdin','-threads','1',*options,
                         '-ss',str(min(8,duration*.15)),'-i',source,'-map','0:V:0',
                         '-frames:v','1','-an','-vf','scale=480:480:force_original_aspect_ratio=decrease',
                         '-threads','1','-q:v','7',str(output)],15,cancelled)
            if not output.is_file() or not 0 < output.stat().st_size <= COVER_MAX_BYTES:
                raise MediaError('封面抽取失败或图片过大')
            return output.read_bytes()


def sidecar_allowed(ol, path, token):
    # Both files inherit the parent policy. Explicit per-file rules and hide
    # expressions can treat the cover differently, so use the private cache.
    for page in range(1, 51):
        response = ol.call('GET',f'admin/meta/list?page={page}&per_page=100',token)
        rows = response['content'] or []
        for row in rows:
            meta_path = row['path'].rstrip('/') or '/'
            if meta_path == path or (row.get('hide') and
               (meta_path=='/' or path.startswith(meta_path+'/'))):
                return False
        if page*100 >= response['total']:
            return True
    return False


def read_sidecar(url, openlist_url):
    try:
        with gateway(url,openlist_url,COVER_MAX_BYTES+1,10) as (source,_):
            with httpx.stream('GET',source,timeout=10,trust_env=False) as response:
                response.raise_for_status()
                data = b''.join(response.iter_bytes())
    except httpx.HTTPError:
        raise MediaError('无法读取封面') from None
    if len(data)>COVER_MAX_BYTES or not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        raise MediaError('无效封面')
    return data


def save_cover(db, path, fingerprint, sidecar, jpeg):
    cache = getattr(db,'cover_cache',db)
    with cache.connect() as connection:
        connection.execute('REPLACE INTO covers(path,fingerprint,sidecar,jpeg,updated) VALUES (?,?,?,?,?)',
                           (path,fingerprint,sidecar,jpeg,time.time()))
        size = connection.execute('SELECT COALESCE(SUM(length(jpeg)),0) FROM covers').fetchone()[0]
        if size > CACHE_MAX_BYTES:
            for row in connection.execute('SELECT path,length(jpeg) AS size FROM covers WHERE jpeg IS NOT NULL ORDER BY updated').fetchall():
                connection.execute('UPDATE covers SET jpeg=NULL WHERE path=?',(row['path'],))
                size -= row['size']
                if size <= CACHE_MAX_BYTES:
                    break
    if cache is not db:
        db.execute('REPLACE INTO covers(path,fingerprint,sidecar,jpeg,updated) VALUES (?,?,?,NULL,?)',
                   (path,fingerprint,sidecar,time.time()))


def cached_cover(db, path, fingerprint):
    cache = getattr(db,'cover_cache',db)
    row = cache.one('SELECT jpeg FROM covers WHERE path=? AND fingerprint=?',(path,fingerprint))
    return row['jpeg'] if row else None


def store_sidecar(ol, path, fingerprint, jpeg, token):
    if not sidecar_allowed(ol,path,token):
        return None
    target = cover_path(path,fingerprint)
    ol.put_cover(target,jpeg,token)
    saved = ol.get(target,token)
    if saved.get('is_dir') or saved.get('size') != len(jpeg):
        raise HTTPException(502,'无法确认封面写入')
    return target
