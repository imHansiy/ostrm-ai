"""A scoped view of existing AI results, shared by every external format."""
import json
import math
import mimetypes
import os
import posixpath
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime,timezone
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException
from .. import config
from ..covers import cached_cover,read_sidecar,save_cover
from ..library import LibraryCatalog,categories,filename_year
from ..media import approve_url,MediaError
from .store import MediaStore,digest


def line(value):
    return re.sub(r'[\x00-\x1f\x7f]', ' ',str(value)).strip()


def timestamp(value,fallback=0):
    try:
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError,TypeError,OverflowError):return fallback


def inside(path,roots):
    return any(root=='/' or path==root or path.startswith(root.rstrip('/')+'/') for root in roots)


def finite(value,maximum=315360000):
    try:value=float(value)
    except (ValueError,TypeError):return 0
    return min(maximum,max(0,value)) if math.isfinite(value) else 0


class MediaCatalog:
    def __init__(self,db,ol):
        self.db=db;self.ol=ol;self.store=MediaStore(db);self.library=LibraryCatalog(db)
        self.lock=threading.RLock();self.cache={};self.tokens={}
        self.artifacts=threading.local()

    @contextmanager
    def artifact_batch(self,items):
        """Bound export reads to one result/cover query per 100 files on TiDB."""
        paths=[item['path'] for item in items]
        previous=getattr(self.artifacts,'rows',None)
        rows=self.db.videos_by_path(paths)
        if paths:
            if self.db.backend=='tidb':
                expression=','.join('UNHEX(SHA2(?,256))' for _ in paths)
                covers=self.db.all(f'SELECT * FROM covers WHERE path_key IN ({expression})',paths)
            else:
                expression=','.join('?' for _ in paths)
                covers=self.db.all(f'SELECT * FROM covers WHERE path IN ({expression})',paths)
        else:covers=[]
        self.artifacts.rows=(rows,{row['path']:row for row in covers})
        try:yield
        finally:self.artifacts.rows=previous

    def invalidate(self):
        with self.lock:self.cache.clear()

    def permitted(self,publication,item):
        adult='成人内容' in categories(item)
        return (inside(item['path'],publication['roots']) and
                (publication['content']=='all' or adult==(publication['content']=='adult')) and
                {tag.casefold() for tag in publication['tags']}.issubset(tag.casefold() for tag in item['tags']))

    def normalize(self,row,index=None):
        data=self.db.public_video(row,self.library.aliases()) if index is None else row
        if index is None:index=self.store.index(row['path'])
        result=data.get('result',{})
        duration=finite(result.get('sampling',{}).get('duration'))
        name=posixpath.splitext(posixpath.basename(data['path']))[0]
        labels=categories(data)
        return {'id':index['id'],'vod_id':index['vod_id'],'path':data['path'],
            'title':line(result.get('title') or name),'summary':str(result.get('summary','')),
            'tags':data['tags'],'ai_tags':data['ai_tags'],'manual_tags':data['manual_tags'],
            'categories':labels,'adult':'成人内容' in labels,'year':filename_year(data['path']),
            'duration':duration,'size':max(0,int(data.get('size',0))),
            'updated':finite(data.get('updated'),100000000000),
            'date':timestamp(data.get('modified'),finite(data.get('updated'),100000000000)),
            'extension':posixpath.splitext(data['path'])[1].lower().lstrip('.'),
            'mime':mimetypes.guess_type(data['path'])[0] or 'application/octet-stream',
            'fingerprint':data.get('fingerprint','')}

    def items(self,publication):
        key=(publication['id'],publication['updated'])
        with self.lock:
            entry=self.cache.get(key)
            if entry and time.monotonic()-entry[0]<10:return entry[1]
            aliases=self.library.aliases();items=[]
            # Compact SQL projection avoids reading model transcripts for feeds.
            for batch in self.library.batches('/','newest'):
                for row in batch:
                    data={**row,**self.db.public_video(row,aliases)}
                    if self.permitted(publication,data):items.append(data)
                    if len(items)>=publication['max_items']:break
                if len(items)>=publication['max_items']:break
            indexes=self.store.index_many([item['path'] for item in items])
            normalized=[self.normalize(item,indexes[item['path']]) for item in items]
            if len(self.cache)>32:self.cache.clear()
            self.cache[key]=(time.monotonic(),normalized)
            return normalized

    def item(self,publication,identifier):
        if len(str(identifier))>64:raise HTTPException(404,'媒体不存在')
        index=self.db.one('SELECT * FROM media_index WHERE '+('vod_id' if str(identifier).isdigit() and len(str(identifier))<17 else 'id')+'=?',(identifier,))
        if not index:raise HTTPException(404,'媒体不存在')
        row=self.db.one("SELECT * FROM videos WHERE path=? AND status='done'",(index['path'],))
        if not row:raise HTTPException(404,'媒体不存在或尚未识别')
        public={**row,**self.db.public_video(row,self.library.aliases())}
        if not self.permitted(publication,public):raise HTTPException(404,'媒体不在当前订阅范围')
        return self.normalize(public,index)

    def full_result(self,item):
        batch=getattr(self.artifacts,'rows',None)
        row=batch[0].get(item['path']) if batch is not None else self.db.one('SELECT result FROM videos WHERE path=?',(item['path'],))
        result=json.loads(row['result']) if row else {}
        # Export useful observations, not raw model responses or private diagnostics.
        return {key:result[key] for key in ('summary','tags','visible_text','segments','characters','events','uncertainty','verification','sampling') if key in result}

    def url(self,publication,kind,item=None,key=None):
        cache_key=(publication['id'],publication['updated'])
        with self.lock:
            if cache_key not in self.tokens:
                if len(self.tokens)>128:self.tokens.clear()
                self.tokens[cache_key]=self.store.secret(publication['id'])
            token=key or self.tokens[cache_key]
        base=publication['public_base_url']+'/media/'+publication['id']
        if kind=='api.php/provide/vod':
            return base+'/provider/'+token+'/api.php/provide/vod'
        suffix={'stream':'/stream/'+item['id']+'/'+quote(posixpath.basename(item['path']),safe=''),
                'image':'/image/'+item['id']+'.jpg','subtitle':'/subtitle/'+item['id']+'.srt'}[kind] if item else '/'+kind
        return base+suffix+'?'+urlencode({'key':token})

    def links(self,publication):
        return {name:self.url(publication,path) for name,path in {
            'strm_nfo_zip':'library.zip','m3u':'playlist.m3u','rss':'rss.xml','podcast':'podcast.xml',
            'maccms':'api.php/provide/vod','tvbox':'tvbox.json','libretv':'libretv.json'}.items()}|{
                'jellyfin_server':publication['public_base_url']+'/media/jellyfin/'+publication['id'],
                'jellyfin_username':publication['id'],'jellyfin_password':self.store.secret(publication['id']),
                'webdav_server':os.getenv('PUBLIC_OPENLIST_URL',config.OPENLIST_URL).rstrip('/')+'/dav',
                'webdav_ai_server':publication['public_base_url']+'/media/dav/'+publication['id']+'/',
                'webdav_username':publication['id'],'webdav_password':self.store.secret(publication['id'])}

    def jpeg(self,item,remote=True):
        batch=getattr(self.artifacts,'rows',None)
        row=batch[1].get(item['path']) if batch is not None else self.db.one('SELECT * FROM covers WHERE path=? AND fingerprint=?',(item['path'],item['fingerprint']))
        if row and row['fingerprint']!=item['fingerprint']:return None
        if not row:return None
        data=row['jpeg'] or cached_cover(self.db,item['path'],item['fingerprint'])
        if not data and remote and row['sidecar']:
            try:
                obj=self.ol.get(row['sidecar'],self.ol.admin_token())
                data=read_sidecar(obj['raw_url'],self.ol.url)
                save_cover(self.db,item['path'],item['fingerprint'],row['sidecar'],data)
            except (HTTPException,RuntimeError,KeyError):return None
        return data

    def playback_url(self,item):
        obj=self.ol.get(item['path'],self.ol.admin_token())
        from ..worker import fingerprint
        if obj.get('is_dir') or fingerprint(obj)!=item['fingerprint']:
            raise HTTPException(409,'原视频已变化，请重新扫描识别')
        target=obj.get('raw_url','')
        try:approve_url(target,self.ol.url)
        except (MediaError,ValueError,OSError):raise HTTPException(502,'媒体地址未获准访问，请检查源站与允许主机') from None
        parsed=urlsplit(target);source=urlsplit(self.ol.url)
        public=os.getenv('PUBLIC_OPENLIST_URL','').rstrip('/')
        if (parsed.scheme,parsed.netloc)==(source.scheme,source.netloc) and public:
            origin=urlsplit(public);target=urlunsplit((origin.scheme,origin.netloc,origin.path.rstrip('/')+parsed.path,parsed.query,''))
        return target

    def subtitle(self,item):
        from ..subtitles import artifact_path,srt
        path=artifact_path(item['path'],item['fingerprint'])
        if not path.is_file() or path.stat().st_size>8*1024*1024:return None
        try:return srt(json.loads(path.read_text(encoding='utf-8'))).encode('utf-8')
        except (ValueError,KeyError,TypeError):return None
