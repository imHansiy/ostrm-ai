import json
import hashlib
import os
import posixpath
import shlex
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from . import config
from .db import Database
from .openlist import OpenList, canonical, relative, clean_path
from .worker import Worker, fingerprint
from .model import infer
from .subtitles import artifact_path, srt
from .covers import cached_cover, read_sidecar, save_cover
from .catalog import CatalogChecks
from .library import LibraryCatalog, filename_year, categories, CATEGORY_TAGS


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_url: str = Field(max_length=500)
    model_name: str = Field(min_length=1,max_length=200)
    model_thinking: bool = True
    video_num_frames: int = Field(default=32,ge=0,le=128)
    context_frames: int = Field(default=5,ge=0,le=8)
    roots: list[str] = Field(min_length=1,max_length=100)
    exclude: list[str] = Field(max_length=100)
    extensions: list[str] = Field(min_length=1,max_length=50)
    interval_minutes: int = Field(ge=0,le=525600)
    concurrency: int = Field(ge=1,le=4)
    frames: int = Field(ge=2,le=48)
    max_video_bytes: int = Field(ge=1024,le=1099511627776)
    max_network_bytes: int = Field(ge=1048576,le=16 * 1024**3)
    media_timeout: int = Field(ge=10,le=7200)
    model_timeout: int = Field(ge=10,le=1800)
    page_size: int = Field(ge=1,le=500)
    max_depth: int = Field(ge=1,le=128)
    max_files: int = Field(ge=1,le=1000000)
    scan_delay: float = Field(ge=.1,le=60)


class Paths(BaseModel):
    paths: list[str] = Field(max_length=200)
    password: str = Field(default='',max_length=500)


class VideoBatch(Paths):
    preview: bool = False


class Recognition(Paths):
    content_type: Literal['auto','movie'] = 'auto'
    use_subtitles: bool = False


class Edit(BaseModel):
    path: str
    manual_tags: list[str] = Field(max_length=50)
    removed: list[str] = Field(default_factory=list,max_length=50)


class ModelKey(BaseModel):
    key: str = Field(max_length=4096)


class NativeSearch(BaseModel):
    parent: str = Field(default='/',max_length=4096)
    keywords: str = Field(default='',max_length=200)
    password: str = Field(default='',max_length=500)
    scope: Literal[0,1,2] = 0
    cursor: int = Field(default=0,ge=0)
    limit: int = Field(default=30,ge=1,le=100)


class LibraryQuery(BaseModel):
    keywords: str = Field(default='',max_length=200)
    tags: list[str] = Field(default_factory=list,max_length=12)
    status: Literal['','done','pending','running','failed','cancelled'] = ''
    password: str = Field(default='',max_length=500)
    cursor: int = Field(default=0,ge=0)
    page: int = Field(default=1,ge=1,le=1000000)
    page_size: int = Field(default=24,ge=1,le=60)
    sort: Literal['newest','oldest','name'] = 'newest'
    category: str = Field(default='',max_length=40)
    content: Literal['all','general','adult'] = 'all'
    year: str = Field(default='',pattern=r'^(?:|unknown|(?:19|20)\d{2})$')
    as_of: float = Field(default=0,ge=0,allow_inf_nan=False)


class CoverBackfill(BaseModel):
    enabled: bool


def create_app(db=None, ol=None, background=True):
    config.DATA.mkdir(parents=True,exist_ok=True)
    if db is None:
        from .storage import open_database
        db=open_database(config.DATA / 'aitags.sqlite3')
    ol = ol or OpenList()
    worker = Worker(db,ol)
    catalog = CatalogChecks(ol)
    library_catalog = LibraryCatalog(db)

    @asynccontextmanager
    async def lifespan(app):
        if background:
            worker.start()
            publisher.start()
        yield
        if background:
            worker.close()
            publisher.close()
        catalog.close()
        ol.close()

    app = FastAPI(title='OStrm AI 识别与外部协议',version='0.2.0',lifespan=lifespan,docs_url=None,redoc_url=None)
    app.state.db, app.state.ol, app.state.worker = db,ol,worker
    app.state.catalog = catalog

    @app.middleware('http')
    async def request_guard(request,call_next):
        if request.url.path.startswith(('/ai/api','/media/')):
            body=bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>65536:
                    return JSONResponse({'detail':'请求过大'},status_code=413)
            request._body=bytes(body)
        if request.url.path.startswith('/media/') and request.method=='OPTIONS':
            response=Response(status_code=204)
        else:
            response = await call_next(request)
        if request.url.path.startswith('/media/'):
            response.headers.update({'Cache-Control':'no-store','Access-Control-Allow-Origin':'*',
                'Access-Control-Allow-Headers':'Authorization,Content-Type,Range,X-Emby-Token,X-MediaBrowser-Token,X-Emby-Authorization',
                'Access-Control-Allow-Methods':'GET,HEAD,POST,DELETE,OPTIONS,PROPFIND',
                'Access-Control-Expose-Headers':'Content-Length,Content-Range,Location,Accept-Ranges'})
            if request.url.path.startswith('/media/dav/'):
                response.headers['DAV']='1'
                response.headers['Allow']='OPTIONS,PROPFIND,GET,HEAD'
        if request.url.path.startswith('/ai/api'):
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def auth(request, admin=False):
        token = request.headers.get('authorization','')
        bridge=request.headers.get('x-ai-bridge','')
        if bridge:
            import hmac
            expected=os.getenv('AI_BRIDGE_KEY','')
            if not expected or not hmac.compare_digest(bridge,expected):raise HTTPException(401,'服务连接凭据无效')
            token=ol.admin_token()
        user = ol.me(token)
        if admin and user.get('role') != 2:
            raise HTTPException(403,'此操作需要 OpenList 管理员权限')
        return token,user

    def listed_objects(rows,token,user,password):
        # Administrators can inspect all files. Directory metadata avoids
        # resolving a cloud download URL for every library card.
        if user.get('role')!=2:
            return {}
        groups=defaultdict(list)
        for row in rows:
            path=relative(user,row['path'])
            if path is not None:
                groups[posixpath.dirname(path)].append(path)
        def listing(parent):
            try:
                data=ol.listing(parent,token,1,500,password,timeout=4)
                objects={posixpath.join(parent,obj['name']):obj for obj in data.get('content') or []
                         if obj.get('name') and '/' not in obj['name'] and '\\' not in obj['name']}
                if data.get('total',0)<=500:
                    for path in groups[parent]:
                        objects.setdefault(path,None)
                return objects
            except HTTPException:
                return dict.fromkeys(groups[parent])
        with ThreadPoolExecutor(max_workers=4) as pool:
            objects={}
            for page in pool.map(listing,groups):
                objects.update(page)
        return objects

    def visible(row, token, user, password='', aliases=None, objects=None):
        path = relative(user,row['path'])
        if path is None:
            return None
        try:
            if objects is not None and path in objects:
                obj=objects[path]
                if obj is None:
                    return None
            else:
                obj = ol.get(path,token,password,timeout=4) if objects is not None else ol.get(path,token,password)
            if obj.get('is_dir'):
                return None
        except HTTPException:
            return None
        result = db.public_video(row,aliases)
        if fingerprint(obj)!=row['fingerprint']:
            result.update(status='pending',result={},ai_tags=[],tags=result['manual_tags'],error='视频已变化，请重新扫描')
        result['path'] = path
        result['file'] = {key:obj.get(key) for key in ('name','size','modified','thumb')}
        return result

    @app.get('/ai/health')
    def health():
        return {'status':'ok','version':'0.2.0','database':db.backend}

    @app.get('/ai/exports',include_in_schema=False)
    def export_page():
        return FileResponse(Path(__file__).parent/'static'/'exports.html',media_type='text/html')

    @app.get('/ai/cinema',include_in_schema=False)
    @app.get('/ai/cinema/',include_in_schema=False)
    def retired_cinema():
        from fastapi.responses import RedirectResponse
        return RedirectResponse('/ai/exports',status_code=307)

    @app.get('/ai/api/me')
    def me(request:Request):
        _,user = auth(request)
        return {'admin':user.get('role')==2,'username':user.get('username'),'extensions':db.settings()['extensions']}

    @app.post('/ai/api/videos/batch')
    def batch(body:VideoBatch,request:Request):
        token,user = auth(request)
        paths=[canonical(user,path) for path in body.paths]
        rows=db.videos_by_path(paths)
        if not rows:
            return []
        aliases={r['alias']:r['canonical'] for r in db.all('SELECT * FROM aliases')}
        objects=listed_objects(rows.values(),token,user,body.password) if body.preview else None
        def fetch(path):
            row=rows.get(path)
            if row:
                item=visible(row,token,user,body.password,aliases,objects)
                if item and body.preview:
                    item['result']={'summary':item['result'].get('summary','')}
                return item
        with ThreadPoolExecutor(max_workers=4) as pool:
            return [item for item in pool.map(fetch,paths) if item]

    @app.post('/ai/api/subtitles')
    def subtitles(body:Paths,request:Request):
        token,user = auth(request)
        if len(body.paths) != 1:
            raise HTTPException(400,'请选择一个视频')
        row = db.one('SELECT * FROM videos WHERE path=?',(canonical(user,body.paths[0]),))
        item = visible(row,token,user,body.password) if row else None
        if not item or not item['result']:
            raise HTTPException(404,'字幕不存在或无权访问')
        artifact = artifact_path(row['path'],row['fingerprint'])
        if not artifact.is_file():
            raise HTTPException(404,'此视频尚未提取字幕')
        return {'srt':srt(json.loads(artifact.read_text(encoding='utf-8')))}

    @app.get('/ai/api/search')
    def search(request:Request,q:str='',tags:str='',cursor:int=0,limit:int=30):
        token,user=auth(request)
        wanted=[x.strip() for x in tags.split(',') if x.strip()]
        if len(q)>200 or len(tags)>1000 or len(wanted)>20 or not 1<=limit<=100 or cursor<0:
            raise HTTPException(400,'搜索参数超出限制')
        # No global count/facets: both can leak protected names or labels.
        aliases={r['alias']:r['canonical'] for r in db.all('SELECT * FROM aliases')}
        rows=db.all('SELECT rowid AS row_cursor,videos.* FROM videos WHERE rowid>? ORDER BY rowid LIMIT 300',(cursor,))
        results=[]
        last=cursor
        for row in rows:
            last=row['row_cursor']
            data=db.public_video(row,aliases)
            hay=' '.join(data['tags'])+' '+data['result'].get('summary','')
            if q.casefold() not in hay.casefold() or not all(t in data['tags'] for t in wanted):
                continue
            item=visible(row,token,user,aliases=aliases)
            if item and q.casefold() in (' '.join(item['tags'])+' '+item['result'].get('summary','')).casefold() and all(t in item['tags'] for t in wanted):
                results.append(item)
            if len(results)>=limit:
                break
        return {'items':results,'next_cursor':last if rows and (len(rows)==300 or len(results)==limit) else None}

    @app.post('/ai/api/native-search')
    def native_search(body:NativeSearch,request:Request):
        token,user=auth(request)
        parent=canonical(user,clean_path(body.parent)).rstrip('/')+'/'
        words=[]
        wanted=[]
        try:
            terms=shlex.split(body.keywords)
        except ValueError:
            terms=body.keywords.split()
        for word in terms:
            if word.startswith('#') and len(word)>1:
                wanted.append(word[1:])
            elif word.startswith(('标签:','标签：')):
                wanted.extend(x for x in word[3:].replace('，',',').split(',') if x)
            else:
                words.append(word.casefold())
        if len(wanted)>20:
            raise HTTPException(400,'组合标签最多20个')
        if body.scope==1 or not (words or wanted):
            return {'items':[],'next_cursor':None}
        aliases={r['alias']:r['canonical'] for r in db.all('SELECT * FROM aliases')}
        wanted=[aliases.get(t,t) for t in wanted]
        folded_aliases={k.casefold():v.casefold() for k,v in aliases.items()}
        words=[folded_aliases.get(w,w) for w in words]
        rows=db.all('SELECT rowid AS row_cursor,videos.* FROM videos WHERE rowid>? AND substr(path,1,?)=? ORDER BY rowid LIMIT 300',
                    (body.cursor,len(parent),parent))
        results=[]
        last=body.cursor
        def matches(item):
            hay=(' '.join(item['tags'])+' '+item['result'].get('summary','')).casefold()
            return all(w in hay for w in words) and all(t in item['tags'] for t in wanted)
        def check(row):
            item=visible(row,token,user,body.password,aliases,objects)
            # Check again after live permission and current-file checks. No stale summaries.
            return item if item and matches(item) else None
        candidates=[row for row in rows if matches(db.public_video(row,aliases))]
        objects={}
        if rows:
            last=rows[-1]['row_cursor']
        if candidates:
            with ThreadPoolExecutor(max_workers=4) as pool:
                for offset in range(0,len(candidates),4):
                    group=candidates[offset:offset+4]
                    missing=[row for row in group if relative(user,row['path']) not in objects]
                    objects.update(listed_objects(missing,token,user,body.password))
                    for row,item in zip(group,pool.map(check,group)):
                        if item:
                            results.append(item)
                        if len(results)>=body.limit:
                            last=row['row_cursor']
                            break
                    if len(results)>=body.limit:
                        break
        return {'items':results,'next_cursor':last if rows and (len(rows)==300 or len(results)==body.limit) else None}

    @app.post('/ai/api/videos/edit')
    def edit(body:Edit,request:Request):
        token,user=auth(request,True)
        ol.get(body.path,token)
        path=canonical(user,body.path)
        if not db.one('SELECT path FROM videos WHERE path=?',(path,)):
            raise HTTPException(404,'请先识别视频')
        def norm(values):
            if any(not t.strip() or len(t.strip())>40 for t in values):
                raise HTTPException(400,'标签应为 1–40 个字符')
            return list(dict.fromkeys(t.strip() for t in values))
        db.execute('UPDATE videos SET manual=?,removed=?,updated=? WHERE path=?',
                   (json.dumps(norm(body.manual_tags),ensure_ascii=False),json.dumps(norm(body.removed),ensure_ascii=False),time.time(),path))
        db.event(path,'edited',f'管理员 {user.get("id")} 修改标签')
        publisher.catalog.invalidate()
        db.execute('UPDATE media_targets SET updated=0 WHERE item_id=?',(hashlib.sha256(path.encode()).hexdigest(),))
        return db.public_video(db.one('SELECT * FROM videos WHERE path=?',(path,)))

    @app.post('/ai/api/scan')
    def scan(body:Recognition,request:Request):
        token,_=auth(request,True)
        for root in body.paths:
            obj=ol.get(root,token,body.password)
            if not obj.get('is_dir'):
                raise HTTPException(400,'扫描入口必须是目录')
        if body.password:
            raise HTTPException(400,'后台扫描使用服务端管理员权限，不保存目录密码')
        return {'ids':[worker.create_run(root,body.content_type,body.use_subtitles) for root in body.paths]}

    @app.post('/ai/api/recognize')
    def recognize(body:Recognition,request:Request):
        token,_=auth(request,True)
        ids=[]
        for path in body.paths:
            path=clean_path(path)
            obj=ol.get(path,token)
            if obj.get('is_dir') or posixpath.splitext(path)[1].lstrip('.').lower() not in db.settings()['extensions']:
                raise HTTPException(400,'仅支持配置的视频格式')
            with db.connect() as c:
                rid=c.execute('INSERT INTO runs(root,created,content_type,use_subtitles) VALUES (?,?,?,?)',
                              (path,time.time(),body.content_type,body.use_subtitles)).lastrowid
            if not worker.enqueue(path,obj,rid,force=True):
                db.execute("UPDATE runs SET state='done' WHERE id=?",(rid,))
            ids.append(rid)
        return {'ids':ids}

    @app.get('/ai/api/overview')
    def overview(request:Request):
        auth(request,True)
        with db.connect() as connection:
            connection.execute('BEGIN')
            videos={row['status']:row['n'] for row in connection.execute(
                'SELECT status, COUNT(*) AS n FROM videos GROUP BY status')}
            tasks={row['state']:row['n'] for row in connection.execute(
                """SELECT t.state, COUNT(*) AS n FROM tasks t JOIN runs r ON r.id=t.run_id
                   WHERE (t.state='queued' AND r.state='running') OR t.state='running'
                   GROUP BY t.state""")}
            scans={row['state']:row['n'] for row in connection.execute(
                "SELECT state, COUNT(*) AS n FROM runs WHERE state IN ('running','paused') GROUP BY state")}
        return {'videos':videos,'tasks':tasks,'scans':scans}

    @app.post('/ai/api/library')
    def library(body:LibraryQuery,request:Request):
        token,user=auth(request,True)
        prefix=canonical(user,'/').rstrip('/')+'/'
        if not body.as_of:
            body=body.model_copy(update={'as_of':time.time()})
        aliases=library_catalog.aliases()
        rows,total,page=library_catalog.page(body,prefix,aliases)
        def card(row):
            item=db.public_video(row,aliases)
            item['path']=relative(user,row['path'])
            availability,obj=catalog.lookup(item['path'],token,body.password)
            if obj is not None and fingerprint(obj)!=row['fingerprint']:
                availability='changed'
                item.update(status='pending',result={},tags=item['manual_tags'])
            if item['status']!='done':
                return None
            cover=row['has_cover']
            if not cover and db.backend=='tidb':
                cover=bool(cached_cover(db,row['path'],row['fingerprint']))
            return {'path':item['path'],'status':item['status'],'tags':item['tags'],
                    'size':row['size'],'modified':row['modified'],
                    'duration':float(item['result'].get('sampling',{}).get('duration',0) or 0),
                    'year':filename_year(row['path']),'categories':categories(item),
                    'summary':item['result'].get('summary',''),
                    'cover':bool(cover and availability!='changed'),'availability':availability}
        items=[]
        for row in rows:
            item=card(row)
            if item is not None:
                items.append(item)
        return {'items':items,'total':total,'page':page,'page_size':body.page_size,
                'total_pages':(total+body.page_size-1)//body.page_size,'as_of':body.as_of,'next_cursor':None}

    @app.get('/ai/api/library/facets')
    def library_facets(request:Request):
        _,user=auth(request,True)
        prefix=canonical(user,'/').rstrip('/')+'/'
        return library_catalog.facets(prefix)

    @app.post('/ai/api/cinema')
    def cinema_catalog(body:LibraryQuery,request:Request):
        token,user=auth(request)
        if user.get('role')==2:
            return library(body,request)
        # Ordinary sessions get only files revalidated with their own token.
        # A bounded cursor scan avoids checking the entire cloud on page load.
        if not body.as_of:
            body=body.model_copy(update={'as_of':time.time()})
        prefix=canonical(user,'/').rstrip('/')+'/'
        aliases=library_catalog.aliases()
        batch=body.model_copy(update={'page':body.cursor//60+1,'page_size':60})
        candidates,total,_=library_catalog.page(batch,prefix,aliases)
        rows=candidates[body.cursor%60:body.cursor%60+body.page_size]
        def check(row):
            item=visible(row,token,user,body.password,aliases)
            if not item or item['status']!='done':
                return None
            cover=db.one('SELECT path FROM covers WHERE path=? AND fingerprint=?',
                         (row['path'],row['fingerprint']))
            return {'path':item['path'],'status':'done','tags':item['tags'],
                    'size':row['size'],'modified':row['modified'],
                    'duration':float(item['result'].get('sampling',{}).get('duration',0) or 0),
                    'year':filename_year(row['path']),'categories':categories(item),
                    'summary':item['result'].get('summary',''),'cover':bool(cover),'availability':'available'}
        with ThreadPoolExecutor(max_workers=4) as pool:
            items=[item for item in pool.map(check,rows) if item]
        cursor=body.cursor+len(rows)
        return {'items':items,'total':None,'total_pages':None,'page':body.page,
                'page_size':body.page_size,'as_of':body.as_of,
                'next_cursor':cursor if rows and cursor<total else None}

    @app.get('/ai/api/cinema/facets')
    def cinema_facets(request:Request):
        _,user=auth(request)
        if user.get('role')==2:
            return library_catalog.facets(canonical(user,'/').rstrip('/')+'/')
        # Do not disclose hidden-library counts or filenames through facets.
        from datetime import datetime
        return {'types':[{'value':label} for label in [*CATEGORY_TAGS,'其他']],
                'years':[{'value':str(year)} for year in range(datetime.now().year,1899,-1)]+[{'value':'unknown'}]}

    @app.post('/ai/api/library/availability')
    def library_availability(body:Paths,request:Request):
        token,user=auth(request,True)
        rows=db.videos_by_path([canonical(user,path) for path in body.paths])
        items=[]
        for row in rows.values():
            path=relative(user,row['path'])
            if path is None:
                continue
            state,obj=catalog.lookup(path,token,body.password)
            item={'path':path,'availability':state}
            if obj is not None and fingerprint(obj)!=row['fingerprint']:
                item.update(availability='changed',manual_tags=json.loads(row['manual']))
            items.append(item)
        return {'items':items}

    @app.put('/ai/api/library/covers')
    def cover_backfill(body:CoverBackfill,request:Request):
        auth(request,True)
        db.save_settings({'cover_backfill':body.enabled})
        return {'enabled':body.enabled}

    @app.post('/ai/api/cinema/cover')
    @app.post('/ai/api/library/cover')
    def library_cover(body:Paths,request:Request):
        token,user=auth(request,request.url.path!='/ai/api/cinema/cover')
        if len(body.paths)!=1:
            raise HTTPException(400,'请选择一个视频')
        path=canonical(user,body.paths[0])
        row=db.one("""SELECT c.* FROM covers c JOIN videos v ON c.path=v.path
                     AND c.fingerprint=v.fingerprint WHERE c.path=?""",(path,))
        if not row:
            raise HTTPException(404,'封面未生成')
        obj=ol.get(body.paths[0],token,body.password)
        if obj.get('is_dir') or fingerprint(obj)!=row['fingerprint']:
            raise HTTPException(404,'封面已失效')
        jpeg=row['jpeg'] or cached_cover(db,path,row['fingerprint'])
        if not jpeg and row['sidecar']:
            sidecar=relative(user,row['sidecar'])
            if not sidecar:
                raise HTTPException(404,'封面不可访问')
            try:
                jpeg=read_sidecar(ol.get(sidecar,token,body.password)['raw_url'],ol.url)
            except (HTTPException, RuntimeError, KeyError):
                raise HTTPException(404,'封面不可读取') from None
            # Cache only after checking the video's current permissions/version.
            save_cover(db,path,row['fingerprint'],row['sidecar'],jpeg)
        if not jpeg:
            raise HTTPException(404,'封面未生成')
        return Response(content=jpeg,media_type='image/jpeg')

    @app.get('/ai/api/jobs')
    def jobs(request:Request):
        auth(request,True)
        rows=db.all('SELECT * FROM runs ORDER BY id DESC LIMIT 100')
        for row in rows:
            row['tasks']={r['state']:r['n'] for r in db.all('SELECT state,count(*) n FROM tasks WHERE run_id=? GROUP BY state',(row['id'],))}
            row['directories']={r['state']:r['n'] for r in db.all('SELECT state,count(*) n FROM directories WHERE run_id=? GROUP BY state',(row['id'],))}
        return rows

    @app.post('/ai/api/jobs/{rid}/{action}')
    def control(rid:int,action:str,request:Request):
        auth(request,True)
        worker.control(rid,action)
        return db.one('SELECT * FROM runs WHERE id=?',(rid,))

    @app.get('/ai/api/settings')
    def settings(request:Request):
        auth(request,True)
        values=db.settings()
        return {'settings':{k:v for k,v in values.items() if k in Settings.model_fields},
                'cover_backfill':bool(values.get('cover_backfill')),
                'model_key_configured': bool(config.MODEL_KEY), 'media_allowed_hosts': config.MEDIA_HOSTS}

    @app.put('/ai/api/settings')
    def save_settings(body:Settings,request:Request):
        auth(request,True)
        values=body.model_dump()
        p=urlsplit(body.model_url)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise HTTPException(400,'模型地址应为无认证信息的 http(s) 基础地址')
        values['roots']=[clean_path(x) for x in values['roots']]
        values['exclude']=[clean_path(x) for x in values['exclude']]
        values['extensions']=list(dict.fromkeys(x.lower().lstrip('.') for x in values['extensions'] if x.lstrip('.').isalnum()))
        if not values['extensions']:
            raise HTTPException(400,'视频格式不能为空')
        db.save_settings(values)
        return settings(request)

    @app.post('/ai/api/model/test')
    def test_model(request:Request):
        auth(request,True)
        # Real vision inference, using a small packaged image (no user video is sent).
        import base64
        image=(Path(__file__).parent/'static'/'connection-test.jpg').read_bytes()
        sample={'duration':1,'network_bytes':0,'frames':[{'second':.5,'data':base64.b64encode(image).decode()}]}
        try:
            return infer(sample,db.settings())
        except RuntimeError as exc:
            raise HTTPException(502,str(exc)) from None

    @app.put('/ai/api/model/key')
    def save_key(body:ModelKey,request:Request):
        auth(request,True)
        # Write-only secret, excluded from database backups and all GET responses.
        import tempfile
        fd,name=tempfile.mkstemp(prefix='.model-secret-',dir=config.DATA)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as file:
                json.dump({'key':body.key},file)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(name,0o600)
            os.replace(name,config.DATA/'model-secret.json')
            config.MODEL_KEY=body.key
        finally:
            if os.path.exists(name):os.unlink(name)
        return {'configured':bool(body.key)}

    @app.get('/ai/api/history')
    def history(request:Request,before:int=9223372036854775807):
        auth(request,True)
        return db.all('SELECT * FROM history WHERE id<? ORDER BY id DESC LIMIT 100',(before,))

    @app.get('/ai/api/tags')
    def tags(request:Request):
        auth(request,True)
        return {'aliases':db.all('SELECT * FROM aliases'), 'favorites':[r['tag'] for r in db.all('SELECT * FROM favorites')]}

    @app.put('/ai/api/tags')
    def manage_tags(body:dict,request:Request):
        auth(request,True)
        aliases=body.get('aliases',[])
        favorites=body.get('favorites',[])
        if not isinstance(aliases,list) or not isinstance(favorites,list) or len(aliases)>1000 or len(favorites)>100:
            raise HTTPException(400,'标签配置超出限制')
        names={a.get('alias') for a in aliases if isinstance(a,dict)}
        for a in aliases:
            if not isinstance(a,dict) or not all(isinstance(a.get(k),str) and 0<len(a[k])<=40 for k in ('alias','canonical')) or a['canonical'] in names:
                raise HTTPException(400,'同义词不能形成链或循环')
        if any(not isinstance(t,str) or not 0<len(t)<=40 for t in favorites):
            raise HTTPException(400,'常用标签格式错误')
        with db.connect() as c:
            c.execute('DELETE FROM aliases')
            c.execute('DELETE FROM favorites')
            c.executemany('REPLACE INTO aliases VALUES (?,?)',[(a['alias'],a['canonical']) for a in aliases])
            c.executemany('REPLACE INTO favorites VALUES (?)',[(t,) for t in favorites])
        app.state.media.invalidate()
        db.execute('UPDATE media_targets SET updated=0')
        return tags(request)

    @app.post('/ai/api/backup')
    def backup(request:Request):
        auth(request,True)
        directory=config.DATA/'backups'
        directory.mkdir(exist_ok=True)
        target=directory/f'aitags-{time.time_ns()}.sqlite3'
        db.backup(target)
        return FileResponse(target,filename=target.name,media_type='application/vnd.sqlite3')

    from .protocols.router import install_media_routes
    publisher=install_media_routes(app,db,ol,lambda request:auth(request,True),worker)
    app.mount('/ai/assets',StaticFiles(directory=Path(__file__).parent/'static'),name='assets')

    if os.getenv('AI_PROXY_ENABLED','false').lower()=='true':
        # Optional local gateway for evaluation; production may use nginx instead.
        import httpx
        @app.api_route('/{path:path}',methods=['GET','HEAD','POST','PUT','DELETE','PATCH','OPTIONS'])
        async def proxy(path:str,request:Request):
            if path=='ai' or path.startswith('ai/'):
                raise HTTPException(404,'AI API 不存在')
            client=httpx.AsyncClient(timeout=60,follow_redirects=False)
            excluded={'host','connection','content-length','accept-encoding'}
            headers={k:v for k,v in request.headers.items() if k.lower() not in excluded}
            headers['accept-encoding']='identity'
            url=ol.url+'/'+path+('?' + request.url.query if request.url.query else '')
            try:
                upstream=await client.send(client.build_request(request.method,url,headers=headers,content=await request.body()),stream=True)
            except httpx.HTTPError:
                await client.aclose()
                raise HTTPException(502,'OpenList 网关连接失败') from None
            response_headers={k:v for k,v in upstream.headers.items() if k.lower() not in {'content-length','content-encoding','transfer-encoding','connection'}}
            if 'text/html' in upstream.headers.get('content-type','') and request.method=='GET':
                data=await upstream.aread()
                await upstream.aclose()
                await client.aclose()
                if len(data)<4*1024*1024:
                    if b'/ai/assets/ai.js' not in data:
                        adapter=b'<link rel="stylesheet" href="/ai/assets/ai.css"><script defer src="/ai/assets/ai.js"></script>'
                        data=data.replace(b'</head>',adapter+b'</head>',1)
                return Response(data,status_code=upstream.status_code,headers=response_headers)
            async def stream():
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await upstream.aclose()
                    await client.aclose()
            return StreamingResponse(stream(),status_code=upstream.status_code,headers=response_headers)
    return app


app=create_app(background=os.getenv('AI_BACKGROUND_ENABLED','true').lower() not in ('0','false','no'))
