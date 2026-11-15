"""Public scoped protocols and administrator-only publication management."""
import io
import json
import os
import posixpath
import time
from urllib.parse import urlsplit
from typing import Literal
from fastapi import HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse,RedirectResponse,Response,StreamingResponse
from pydantic import BaseModel,Field
from .catalog import MediaCatalog
from .publisher import Publisher,basename
from .store import Publication,digest
from . import formats


class OStrmTarget(BaseModel):
    path: str=Field(min_length=1,max_length=4096)
    directory: str=Field(default='',max_length=4096)
    basename: str=Field(min_length=1,max_length=150)
    source_base: str=Field(min_length=1,max_length=1000)
    source_size: int|None=Field(default=None,ge=0)
    source_modified: str|None=Field(default=None,max_length=200)
    enqueue: bool=True


def install_media_routes(app,db,ol,admin_auth,worker):
    catalog=MediaCatalog(db,ol);publisher=Publisher(catalog)
    app.state.media=catalog;app.state.publisher=publisher
    worker.on_result=publisher.publish_path

    def authorized(request,identifier,path_key=None):
        key=path_key or request.query_params.get('key','')
        if not key and request.headers.get('authorization','').lower().startswith('bearer '):key=request.headers['authorization'][7:]
        return catalog.store.authenticate(identifier,key)

    @app.get('/ai/api/exports/publications')
    def publications(request:Request):
        admin_auth(request)
        return {'items':[catalog.store.unpack(row) for row in db.all('SELECT * FROM media_publications ORDER BY created DESC')],
                'export_directory':str(publisher.root),'openlist_url':os.getenv('PUBLIC_OPENLIST_URL',ol.url),
                'protocols':['strm+nfo','m3u','rss+media-rss','maccms-v10','tvbox','webdav','jellyfin-direct-play']}

    @app.post('/ai/api/exports/publications')
    def create(body:Publication,request:Request):
        admin_auth(request)
        result=catalog.store.create(body,str(request.base_url).rstrip('/'))
        return {**result,'links':catalog.links(result)}

    @app.get('/ai/api/exports/publications/{identifier}/links')
    def links(identifier:str,request:Request):
        admin_auth(request);return catalog.links(catalog.store.get(identifier))

    @app.post('/ai/api/exports/publications/{identifier}/revoke')
    def revoke(identifier:str,request:Request):
        admin_auth(request);catalog.store.revoke(identifier);catalog.invalidate();return {'revoked':True}

    @app.post('/ai/api/exports/publications/{identifier}/rotate')
    def rotate(identifier:str,request:Request):
        admin_auth(request);result=catalog.store.rotate(identifier);catalog.invalidate();return {**result,'links':catalog.links(result)}

    @app.post('/ai/api/exports/publications/{identifier}/sync')
    def sync(identifier:str,request:Request):
        admin_auth(request);return publisher.enqueue(catalog.store.get(identifier,True))

    @app.get('/ai/api/exports/jobs')
    def jobs(request:Request):
        admin_auth(request);return db.all('SELECT * FROM media_export_jobs ORDER BY created DESC LIMIT 100')

    @app.post('/ai/api/exports/backup')
    def backup(request:Request):
        admin_auth(request)
        import tempfile,zipfile
        from pathlib import Path
        output=tempfile.SpooledTemporaryFile(max_size=8*1024**2)
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'aitags.sqlite3';db.backup(target)
            with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
                archive.write(target,'aitags.sqlite3')
                archive.writestr('media-secret.key',catalog.store.key)
                archive.writestr('RESTORE.txt','停止服务，将 aitags.sqlite3 和 media-secret.key 一起恢复到 AI_DATA_DIR。TiDB 安装请使用迁移工具恢复数据库。此备份不包含模型密钥，请妥善保管。')
        output.seek(0)
        def chunks():
            try:
                while data:=output.read(65536):yield data
            finally:output.close()
        return StreamingResponse(chunks(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="ostrm-ai-backup.zip"'})

    @app.get('/ai/api/exports/publications/{identifier}/preview')
    def preview(identifier:str,request:Request):
        admin_auth(request);publication=catalog.store.get(identifier)
        items=catalog.items(publication)
        return {'total':len(items),'items':items[:100]}

    @app.post('/ai/api/ostrm/metadata')
    def metadata(body:OStrmTarget,request:Request):
        admin_auth(request)
        from ..openlist import clean_path
        from .. import config
        expected={ol.url.rstrip('/'),os.getenv('PUBLIC_OPENLIST_URL','').rstrip('/')}
        if body.source_base.rstrip('/') not in expected:raise HTTPException(409,'当前 AI 服务连接了其他 OpenList 实例，请配置对应的 AI 服务')
        path=clean_path(body.path)
        if worker.excluded(path,db.settings()):raise HTTPException(403,'视频位于 AI 排除目录')
        if posixpath.splitext(path)[1].lstrip('.').lower() not in db.settings()['extensions']:raise HTTPException(400,'文件不是已启用的视频格式')
        if basename(body.basename)!=body.basename:raise HTTPException(400,'视频基础文件名不合法')
        publisher.directory(body.directory)
        index=catalog.store.index(path);target_id=digest(path+'\0'+body.directory+'\0'+body.basename)
        db.execute('REPLACE INTO media_targets(id,item_id,directory,basename,updated,error) VALUES (?,?,?,?,?,?)',
            (target_id,index['id'],body.directory,body.basename,0,''))
        row=db.one('SELECT * FROM videos WHERE path=?',(path,))
        obj=None
        if row and row['status']=='done':
            if body.source_size is not None and body.source_modified is not None:
                unchanged=body.source_size==row['size'] and body.source_modified==row['modified']
            else:
                from ..worker import fingerprint
                obj=ol.get(path,ol.admin_token());unchanged=fingerprint(obj)==row['fingerprint']
        else:unchanged=False
        if row and row['status']=='done' and unchanged:
            publisher.publish_path(path)
            target=db.one('SELECT * FROM media_targets WHERE id=?',(target_id,))
            return {'state':'done','target':target_id,'publication':json.loads(target['error']) if target['error'].startswith('{') else {'error':target['error']}}
        if body.enqueue:
            obj=obj or ol.get(path,ol.admin_token())
            if obj.get('is_dir'):raise HTTPException(400,'请选择视频文件')
            with db.connect() as connection:
                active=connection.execute("SELECT id FROM runs WHERE root='[OStrm AI 导入]' AND state IN ('running','paused')").fetchone()
                run_id=active['id'] if active else connection.execute('INSERT INTO runs(root,created) VALUES (?,?)',('[OStrm AI 导入]',time.time())).lastrowid
            worker.enqueue(path,obj,run_id)
            if db.one("SELECT path FROM videos WHERE path=? AND status='done'",(path,)):
                publisher.publish_path(path);return {'state':'done','target':target_id}
        elif row and row['status']=='done' and not unchanged:
            db.execute("UPDATE videos SET status='pending',error=?,updated=? WHERE path=?",('源视频已变化，请重新识别',time.time(),path))
            catalog.invalidate()
        return {'state':'queued' if body.enqueue else 'pending','target':target_id}

    @app.api_route('/media/{identifier}/playlist.m3u',methods=['GET','HEAD'])
    def playlist(identifier:str,request:Request):
        publication=authorized(request,identifier)
        data=formats.m3u(catalog,publication,catalog.items(publication))
        return Response(data if request.method=='GET' else b'',media_type='audio/x-mpegurl; charset=utf-8',headers={'Content-Length':str(len(data))})

    @app.api_route('/media/{identifier}/rss.xml',methods=['GET','HEAD'])
    @app.api_route('/media/{identifier}/podcast.xml',methods=['GET','HEAD'])
    def rss(identifier:str,request:Request):
        publication=authorized(request,identifier)
        try:data=formats.rss(catalog,publication,catalog.items(publication),request.url.path.endswith('podcast.xml'))
        except ValueError as error:raise HTTPException(400,str(error)) from None
        return Response(data if request.method=='GET' else b'',media_type='application/rss+xml; charset=utf-8',headers={'Content-Length':str(len(data))})

    @app.api_route('/media/{identifier}/podcast-art.jpg',methods=['GET','HEAD'])
    def podcast_art(identifier:str,request:Request):
        publication=authorized(request,identifier);image=None
        for item in catalog.items(publication)[:20]:
            image=catalog.jpeg(item,False)
            if image:break
        if not image:raise HTTPException(404,'需要先为播客条目生成封面')
        from .. import config
        import subprocess
        try:
            output=subprocess.run([config.FFMPEG,'-v','error','-threads','1','-filter_threads','1','-protocol_whitelist','pipe',
                '-i','pipe:0','-vf','scale=1400:1400:force_original_aspect_ratio=increase,crop=1400:1400',
                '-frames:v','1','-f','image2pipe','-vcodec','mjpeg','pipe:1'],input=image,capture_output=True,timeout=10)
            if output.returncode or not output.stdout or len(output.stdout)>4*1024**2:raise ValueError('Invalid image')
        except (OSError,ValueError,subprocess.TimeoutExpired):raise HTTPException(422,'封面图像格式无效或处理超时') from None
        return Response(output.stdout if request.method=='GET' else b'',media_type='image/jpeg',headers={'Content-Length':str(len(output.stdout))})

    @app.get('/media/{identifier}/tvbox.json')
    @app.get('/media/{identifier}/tvbox-split.json')
    def tvbox(identifier:str,request:Request):
        return formats.tvbox(catalog,authorized(request,identifier),split=request.url.path.endswith('/tvbox-split.json'))

    @app.get('/media/{identifier}/libretv.json')
    def libretv(identifier:str,request:Request):
        return formats.libretv(catalog,authorized(request,identifier))

    @app.get('/media/{identifier}/api.php/provide/vod')
    @app.get('/media/{identifier}/provider/{provider_key}/api.php/provide/vod')
    @app.get('/media/{identifier}/api.php/provide/vod/{content_scope}')
    @app.get('/media/{identifier}/provider/{provider_key}/api.php/provide/vod/{content_scope}')
    def cms(identifier:str,request:Request,provider_key:str|None=None,content_scope:Literal['general','adult']|None=None):
        publication=authorized(request,identifier,provider_key)
        items=catalog.items(publication);params=request.query_params
        if content_scope is not None:
            items=[item for item in items if item['adult']==(content_scope=='adult')]
        all_classes=formats.cms_classes(items)
        if params.get('wd'):
            words=params['wd'].casefold().split()
            items=[item for item in items if all(word in (' '.join([item['title'],item['summary'],*item['tags']])).casefold() for word in words)]
        if params.get('t'):
            items=[item for item in items if str(formats.cms_item(catalog,publication,item)['type_id'])==params['t']]
        if params.get('ids'):
            ids=set(params['ids'].split(','));items=[item for item in items if str(item['vod_id']) in ids or item['id'] in ids]
        try:page=max(1,min(1000000,int(params.get('pg','1'))));limit=max(1,min(100,int(params.get('limit','20'))))
        except ValueError:raise HTTPException(400,'页码或数量无效') from None
        total=len(items)
        return {'code':1,'msg':'数据列表','page':page,'pagecount':max(1,(total+limit-1)//limit),'limit':limit,'total':total,
            'class':all_classes,'list':[formats.cms_item(catalog,publication,item) for item in items[(page-1)*limit:page*limit]]}

    @app.get('/media/{identifier}/library.zip')
    def archive(identifier:str,request:Request):
        publication=authorized(request,identifier);output=publisher.zip(publication)
        def stream():
            try:
                while chunk:=output.read(65536):yield chunk
            finally:output.close()
        return StreamingResponse(stream(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="ostrm-ai-library.zip"'})

    @app.api_route('/media/{identifier}/stream/{item_id}/{filename:path}',methods=['GET','HEAD'])
    def stream(identifier:str,item_id:str,filename:str,request:Request):
        publication=authorized(request,identifier);item=catalog.item(publication,item_id)
        return RedirectResponse(catalog.playback_url(item),status_code=307)

    @app.api_route('/media/{identifier}/image/{item_id}.jpg',methods=['GET','HEAD'])
    def image(identifier:str,item_id:str,request:Request):
        publication=authorized(request,identifier);item=catalog.item(publication,item_id)
        jpeg=catalog.jpeg(item)
        if not jpeg:raise HTTPException(404,'封面未生成')
        return Response(jpeg if request.method=='GET' else b'',media_type='image/jpeg',headers={'Content-Length':str(len(jpeg))})

    @app.get('/media/{identifier}/subtitle/{item_id}.srt')
    def subtitle(identifier:str,item_id:str,request:Request):
        publication=authorized(request,identifier);item=catalog.item(publication,item_id)
        data=catalog.subtitle(item)
        if data is None:raise HTTPException(404,'字幕未提取')
        return Response(data,media_type='application/x-subrip; charset=utf-8')

    from .jellyfin import install_jellyfin
    install_jellyfin(app,catalog)
    from .webdav import install_webdav
    install_webdav(app,catalog)
    return publisher
