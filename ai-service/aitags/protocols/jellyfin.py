"""Jellyfin 10.11 direct-play media interface; no player or transcoder is shipped."""
import hashlib
import json
import re
import secrets
import threading
import time
from datetime import datetime,timezone
from urllib.parse import urlencode
from fastapi import APIRouter,HTTPException,Request
from fastapi.responses import RedirectResponse,Response
from .catalog import finite
from .store import digest


def utc(seconds):return datetime.fromtimestamp(seconds,timezone.utc).isoformat().replace('+00:00','Z')


def auth_token(request):
    token=request.headers.get('x-emby-token') or request.headers.get('x-mediabrowser-token') or request.query_params.get('api_key') or request.query_params.get('ApiKey')
    if token:return token
    header=request.headers.get('authorization','') or request.headers.get('x-emby-authorization','')
    match=re.search(r'\bToken="([^"]+)"',header,re.I)
    if match:return match[1]
    return header[7:] if header.lower().startswith('bearer ') else ''


async def json_body(request):
    try:body=await request.json()
    except ValueError:raise HTTPException(400,'请求内容必须是 JSON 对象') from None
    if not isinstance(body,dict):raise HTTPException(400,'请求内容必须是 JSON 对象')
    return body


def install_jellyfin(app,catalog):
    router=APIRouter(prefix='/media/jellyfin/{publication_id}')
    store=catalog.store;db=catalog.db;server_id=hashlib.sha256(store.key).hexdigest()[:32]
    attempts={};attempt_lock=threading.Lock()
    def authenticated(publication_id,request):return store.authenticate_session(publication_id,auth_token(request))
    def user_id(publication):return digest('user:'+publication['id'])[:32]
    def check_user(publication,identifier):
        if identifier!=user_id(publication):raise HTTPException(404,'用户不存在')
    def server(publication):
        return {'ServerName':'OStrm AI','Version':'10.11.0','ProductName':'OStrm AI Direct Play',
            'Id':server_id,'StartupWizardCompleted':True,'LocalAddress':publication['public_base_url']+'/media/jellyfin/'+publication['id']}
    def user(publication):
        return {'Name':publication['name'],'Id':user_id(publication),'ServerId':server_id,
            'HasPassword':True,'HasConfiguredPassword':True,'HasConfiguredEasyPassword':False,
            'EnableAutoLogin':False,'Configuration':{'AudioLanguagePreference':'chi','SubtitleLanguagePreference':'chi',
                'DisplayMissingEpisodes':False,'SubtitleMode':'Default','OrderedViews':['library'],'EnableNextEpisodeAutoPlay':True},
            'Policy':{'IsAdministrator':False,'IsHidden':True,'IsDisabled':False,'EnableMediaPlayback':True,
                'EnableAudioPlaybackTranscoding':False,'EnableVideoPlaybackTranscoding':False,'EnablePlaybackRemuxing':False,
                'EnableContentDeletion':False,'EnableAllFolders':True,'EnableAllDevices':True,'EnableRemoteAccess':True,
                'AuthenticationProviderId':'OStrmAI','PasswordResetProviderId':'OStrmAI'}}
    def base(publication):return publication['public_base_url']+'/media/jellyfin/'+publication['id']
    def library(publication):
        return {'Id':'library','ServerId':server_id,'Name':publication['name'],'Type':'CollectionFolder',
            'IsFolder':True,'CollectionType':'homevideos','MediaType':'Unknown','LocationType':'Virtual'}
    def states(publication,request):
        if not hasattr(request.state,'jellyfin_states'):
            request.state.jellyfin_states={row['item_id']:row for row in db.all('SELECT * FROM media_user_state WHERE publication_id=?',(publication['id'],))}
        return request.state.jellyfin_states
    def state_data(publication,identifier,request=None):
        if request is None:return store.user_state(publication['id'],identifier)
        return states(publication,request).get(identifier,{'position_ticks':0,'played':0,'favorite':0,'play_count':0,'last_played':0})
    def state(publication,item,request=None):
        data=state_data(publication,item['id'],request)
        result={'PlaybackPositionTicks':data['position_ticks'],'PlayCount':data['play_count'],
            'IsFavorite':bool(data['favorite']),'Played':bool(data['played']),'Key':item['id'],'ItemId':item['id']}
        if data['last_played']:result['LastPlayedDate']=utc(data['last_played'])
        if item['duration']>0:result['PlayedPercentage']=min(100,data['position_ticks']/1e7/item['duration']*100)
        return result
    def dto(publication,item,details=False,request=None):
        result={'Id':item['id'],'ServerId':server_id,'Name':item['title'],'SortName':item['title'],
            'Type':'Movie' if '电影' in item['categories'] else 'Video','MediaType':'Video',
            'IsFolder':False,'ParentId':'library','Overview':item['summary'],'Genres':item['categories'],
            'Tags':item['tags'],'RunTimeTicks':round(item['duration']*1e7),'Size':item['size'],
            'Container':item['extension'],'DateCreated':utc(item['updated']),
            'ImageTags':{'Primary':digest(item['fingerprint']+str(item['updated']))[:32]},
            'BackdropImageTags':[],'LocationType':'FileSystem','UserData':state(publication,item,request),
            'CanDelete':False,'CanDownload':True,'VideoType':'VideoFile',
            'ProviderIds':{'OSTrMAI':item['id']},'People':[],'Studios':[]}
        if item['year']!='unknown':result['ProductionYear']=int(item['year'])
        if details:result['MediaSources']=[media_source(publication,item,request)]
        return result
    def media_source(publication,item,request):
        token=auth_token(request) if request else ''
        url=base(publication)+'/Videos/'+item['id']+'/stream.'+(item['extension'] or 'mp4')
        if token:url+='?'+urlencode({'api_key':token,'Static':'true'})
        probe=get_probe(item) if request else None
        streams=(probe or {}).get('streams',[])
        if not streams:streams=[{'Type':'Video','Index':0,'IsDefault':True}]
        if catalog.subtitle(item):
            index=max(stream.get('Index',0) for stream in streams)+1
            streams=[*streams,{'Type':'Subtitle','Codec':'subrip','Index':index,'Language':'chi',
                'IsExternal':True,'IsTextSubtitleStream':True,'SupportsExternalStream':True,
                'DeliveryMethod':'External','DeliveryUrl':base(publication)+'/Videos/'+item['id']+'/'+item['id']+'/Subtitles/'+str(index)+'/Stream.srt?'+urlencode({'api_key':token})}]
        audio=next((stream['Index'] for stream in streams if stream.get('Type')=='Audio'),None)
        result={'Id':item['id'],'Name':item['title'],'Path':url,'Protocol':'Http','Type':'Default',
            'Container':item['extension'],'Size':item['size'],'RunTimeTicks':round(item['duration']*1e7),
            'SupportsDirectPlay':True,'SupportsDirectStream':True,'SupportsTranscoding':False,
            'IsRemote':True,'IsInfiniteStream':False,'RequiresOpening':False,'RequiresClosing':False,
            'ReadAtNativeFramerate':False,'DirectStreamUrl':url,'MediaStreams':streams}
        if audio is not None:result['DefaultAudioStreamIndex']=audio
        if probe and probe.get('bitrate'):result['Bitrate']=probe['bitrate']
        return result

    probe_slots=threading.BoundedSemaphore(2)
    def get_probe(item):
        cached=db.one('SELECT data FROM media_probes WHERE id=? AND fingerprint=?',(item['id'],item['fingerprint']))
        if cached:return json.loads(cached['data'])
        if not probe_slots.acquire(blocking=False):return None
        try:
            from ..media import gateway
            from .. import config
            import subprocess
            source=catalog.playback_url(item)
            with gateway(source,catalog.ol.url,16*1024**2,15) as (url,_):
                completed=subprocess.run([config.FFPROBE,'-v','error','-probesize','1048576','-analyzeduration','1000000',
                    '-show_format','-show_streams','-of','json',url],capture_output=True,timeout=15)
            if completed.returncode:return None
            raw=json.loads(completed.stdout);streams=[]
            for stream in raw.get('streams',[]):
                kind={'video':'Video','audio':'Audio','subtitle':'Subtitle'}.get(stream.get('codec_type'))
                if not kind:continue
                entry={'Type':kind,'Index':int(stream['index']),'Codec':stream.get('codec_name',''),
                    'IsDefault':bool(stream.get('disposition',{}).get('default')),'IsForced':bool(stream.get('disposition',{}).get('forced')),
                    'IsExternal':False,'Language':stream.get('tags',{}).get('language','und')}
                for source_key,target in [('width','Width'),('height','Height'),('channels','Channels'),('sample_rate','SampleRate'),('bit_rate','BitRate'),('level','Level')]:
                    if str(stream.get(source_key,'')).isdigit():entry[target]=int(stream[source_key])
                if stream.get('profile'):entry['Profile']=stream['profile']
                if stream.get('pix_fmt'):entry['PixelFormat']=stream['pix_fmt']
                streams.append(entry)
            result={'streams':streams,'bitrate':int(finite(raw.get('format',{}).get('bit_rate'),1e12))}
            db.execute('REPLACE INTO media_probes(id,fingerprint,data,updated) VALUES (?,?,?,?)',
                (item['id'],item['fingerprint'],json.dumps(result),time.time()))
            return result
        except Exception:return None
        finally:probe_slots.release()

    @router.get('/System/Info/Public')
    def public_info(publication_id:str):return server(store.get(publication_id,True))

    @router.get('/System/Info')
    @router.get('/System/Endpoint')
    def info(publication_id:str,request:Request):
        publication=authenticated(publication_id,request)
        return {'IsLocal':False,'IsInNetwork':False} if request.url.path.endswith('/Endpoint') else server(publication)

    @router.get('/Users/Public')
    def public_users(publication_id:str):store.get(publication_id,True);return []

    @router.post('/Users/AuthenticateByName')
    async def login(publication_id:str,request:Request):
        scope=(publication_id,request.client.host if request.client else '')
        with attempt_lock:
            now=time.monotonic();recent=[moment for moment in attempts.get(scope,[]) if now-moment<60]
            if len(recent)>=20:raise HTTPException(429,'登录尝试过多，请稍后重试')
            recent.append(now)
            if len(attempts)>1000:attempts.clear()
            attempts[scope]=recent
        body=await json_body(request)
        if body.get('Username')!=publication_id:raise HTTPException(401,'用户名或密码错误')
        publication=store.authenticate(publication_id,body.get('Pw') or body.get('Password') or '')
        header=request.headers.get('authorization','') or request.headers.get('x-emby-authorization','')
        match=re.search(r'DeviceId="([^"]+)"',header,re.I);device=match[1] if match else ''
        token=store.session(publication,device)
        return {'User':user(publication),'AccessToken':token,'ServerId':server_id,
            'SessionInfo':{'Id':secrets.token_hex(16),'UserId':user_id(publication),'UserName':publication['name'],'DeviceId':device,'ServerId':server_id,'SupportsRemoteControl':False}}

    @router.get('/Users/Me')
    def me(publication_id:str,request:Request):return user(authenticated(publication_id,request))

    @router.get('/Users')
    def users(publication_id:str,request:Request):return [user(authenticated(publication_id,request))]

    @router.get('/Users/{identifier}')
    def get_user(publication_id:str,identifier:str,request:Request):
        publication=authenticated(publication_id,request);check_user(publication,identifier);return user(publication)

    @router.get('/Users/{identifier}/Views')
    def views(publication_id:str,identifier:str,request:Request):
        publication=authenticated(publication_id,request);check_user(publication,identifier)
        return {'Items':[library(publication)],'TotalRecordCount':1,'StartIndex':0}

    @router.get('/UserViews')
    def modern_views(publication_id:str,request:Request,userId:str|None=None):
        publication=authenticated(publication_id,request)
        if userId:check_user(publication,userId)
        return {'Items':[library(publication)],'TotalRecordCount':1,'StartIndex':0}

    def selected(publication,request):
        params={key.casefold():value for key,value in request.query_params.items()}
        items=catalog.items(publication)
        if params.get('parentid') not in (None,'','library'):return [],0
        if params.get('searchterm'):
            words=params['searchterm'].casefold().split();items=[item for item in items if all(word in ' '.join([item['title'],item['summary'],*item['tags']]).casefold() for word in words)]
        if params.get('ids'):
            identifiers=set(params['ids'].split(','));items=[item for item in items if item['id'] in identifiers]
        if params.get('genres'):items=[item for item in items if set(params['genres'].split('|')).intersection(item['categories'])]
        if params.get('tags'):items=[item for item in items if set(params['tags'].split('|')).issubset(item['tags'])]
        if params.get('years'):items=[item for item in items if item['year'] in params['years'].split(',')]
        if params.get('isplayed') in ('true','false'):items=[item for item in items if bool(state_data(publication,item['id'],request)['played'])==(params['isplayed']=='true')]
        if params.get('isfavorite')=='true' or 'IsFavorite' in params.get('filters',''):items=[item for item in items if state_data(publication,item['id'],request)['favorite']]
        include=params.get('includeitemtypes','').split(',')
        if params.get('includeitemtypes'):items=[item for item in items if ('Movie' if '电影' in item['categories'] else 'Video') in include]
        sort=params.get('sortby','DateCreated').split(',')[0]
        key={'SortName':lambda item:item['title'].casefold(),'Name':lambda item:item['title'].casefold(),
             'ProductionYear':lambda item:item['year'],'DateCreated':lambda item:item['updated'],
             'DatePlayed':lambda item:state_data(publication,item['id'],request)['last_played']}.get(sort,lambda item:item['updated'])
        return sorted(items,key=key,reverse=params.get('sortorder','Descending')=='Descending'),params

    def query(publication,request,resume=False,latest=False):
        items,params=selected(publication,request)
        if not isinstance(params,dict):params={}
        if resume:items=[item for item in items if state_data(publication,item['id'],request)['position_ticks']>0 and not state_data(publication,item['id'],request)['played']]
        try:start=max(0,int(params.get('startindex',0)));limit=max(1,min(200,int(params.get('limit',24))))
        except ValueError:raise HTTPException(400,'分页参数无效') from None
        results=[dto(publication,item,request=request) for item in items[start:start+limit]]
        return results if latest else {'Items':results,'TotalRecordCount':len(items),'StartIndex':start}

    @router.get('/Items')
    @router.get('/Users/{identifier}/Items')
    def items(publication_id:str,request:Request,identifier:str|None=None):
        publication=authenticated(publication_id,request)
        if identifier:check_user(publication,identifier)
        return query(publication,request)

    @router.get('/Items/Resume')
    def resume(publication_id:str,request:Request):return query(authenticated(publication_id,request),request,True)

    @router.get('/Users/{identifier}/Items/Latest')
    def latest(publication_id:str,identifier:str,request:Request):
        publication=authenticated(publication_id,request);check_user(publication,identifier);return query(publication,request,latest=True)

    @router.get('/Items/{item_id}')
    @router.get('/Users/{identifier}/Items/{item_id}')
    def item(publication_id:str,item_id:str,request:Request,identifier:str|None=None):
        publication=authenticated(publication_id,request)
        if identifier:check_user(publication,identifier)
        if item_id=='library':return library(publication)
        return dto(publication,catalog.item(publication,item_id),True,request)

    @router.get('/Items/{item_id}/Images/Primary')
    def image(publication_id:str,item_id:str,request:Request):
        publication=authenticated(publication_id,request);data=catalog.jpeg(catalog.item(publication,item_id))
        if not data:raise HTTPException(404,'封面未生成')
        return Response(data,media_type='image/jpeg')

    @router.api_route('/Items/{item_id}/PlaybackInfo',methods=['GET','POST'])
    def playback(publication_id:str,item_id:str,request:Request):
        publication=authenticated(publication_id,request);item=catalog.item(publication,item_id)
        return {'MediaSources':[media_source(publication,item,request)],'PlaySessionId':secrets.token_hex(16)}

    @router.api_route('/Videos/{item_id}/stream',methods=['GET','HEAD'])
    @router.api_route('/Videos/{item_id}/stream.{extension}',methods=['GET','HEAD'])
    def stream(publication_id:str,item_id:str,request:Request,extension:str|None=None):
        publication=authenticated(publication_id,request);return RedirectResponse(catalog.playback_url(catalog.item(publication,item_id)),status_code=307)

    @router.get('/Videos/{item_id}/{source_id}/Subtitles/{index}/Stream.{extension}')
    def subtitle(publication_id:str,item_id:str,source_id:str,index:int,extension:str,request:Request):
        publication=authenticated(publication_id,request);data=catalog.subtitle(catalog.item(publication,item_id))
        if data is None:raise HTTPException(404,'字幕未提取')
        if extension=='vtt':data=b'WEBVTT\n\n'+re.sub(rb'(\d\d:\d\d:\d\d),(\d{3})',rb'\1.\2',data)
        elif extension!='srt':raise HTTPException(400,'支持 SRT 和 VTT 字幕')
        return Response(data,media_type='text/vtt; charset=utf-8' if extension=='vtt' else 'application/x-subrip; charset=utf-8')

    @router.post('/Sessions/Playing')
    @router.post('/Sessions/Playing/Progress')
    @router.post('/Sessions/Playing/Stopped')
    async def progress(publication_id:str,request:Request):
        publication=authenticated(publication_id,request);body=await json_body(request);item=catalog.item(publication,str(body.get('ItemId','')))
        position=int(finite(body.get('PositionTicks'),1e18));previous=store.user_state(publication_id,item['id'])
        duration=round(item['duration']*1e7)
        if duration:position=min(position,duration)
        ended=request.url.path.endswith('/Stopped') and duration>0 and position>=duration*.9
        store.update_state(publication_id,item['id'],position_ticks=position,last_played=time.time(),
            played=previous['played'] or ended,play_count=previous['play_count']+int(ended and not previous['played']))
        return Response(status_code=204)

    @router.get('/UserItems/{item_id}/UserData')
    @router.get('/Users/{identifier}/Items/{item_id}/UserData')
    def userdata(publication_id:str,item_id:str,request:Request,identifier:str|None=None):
        publication=authenticated(publication_id,request)
        if identifier:check_user(publication,identifier)
        return state(publication,catalog.item(publication,item_id))

    @router.post('/Users/{identifier}/FavoriteItems/{item_id}')
    @router.delete('/Users/{identifier}/FavoriteItems/{item_id}')
    def favorite(publication_id:str,identifier:str,item_id:str,request:Request):
        publication=authenticated(publication_id,request);check_user(publication,identifier);item=catalog.item(publication,item_id)
        store.update_state(publication_id,item_id,favorite=request.method=='POST');return state(publication,item)

    @router.post('/Users/{identifier}/PlayedItems/{item_id}')
    @router.delete('/Users/{identifier}/PlayedItems/{item_id}')
    def played(publication_id:str,identifier:str,item_id:str,request:Request):
        publication=authenticated(publication_id,request);check_user(publication,identifier);item=catalog.item(publication,item_id)
        store.update_state(publication_id,item_id,played=request.method=='POST',position_ticks=0);return state(publication,item)

    @router.get('/Genres')
    @router.get('/Tags')
    def genres(publication_id:str,request:Request):
        publication=authenticated(publication_id,request);items=catalog.items(publication)
        values=sorted({value for item in items for value in item['tags' if request.url.path.endswith('/Tags') else 'categories']})
        return {'Items':[{'Id':digest(value),'Name':value,'Type':'Genre'} for value in values],'TotalRecordCount':len(values),'StartIndex':0}

    @router.get('/Items/Filters')
    @router.get('/Items/Filters2')
    def filters(publication_id:str,request:Request):
        publication=authenticated(publication_id,request);items=catalog.items(publication)
        genres=sorted({genre for item in items for genre in item['categories']})
        return {'Genres':[{'Name':genre,'Id':digest(genre)} for genre in genres] if request.url.path.endswith('Filters2') else genres,
                'Tags':sorted({tag for item in items for tag in item['tags']}),
                'Years':sorted({int(item['year']) for item in items if item['year']!='unknown'},reverse=True)}

    @router.get('/DisplayPreferences/{preference_id}')
    @router.post('/DisplayPreferences/{preference_id}')
    async def preferences(publication_id:str,preference_id:str,request:Request):
        authenticated(publication_id,request)
        if len(preference_id)>200:raise HTTPException(400,'偏好 ID 过长')
        if request.method=='POST':
            data=await json_body(request);db.execute('REPLACE INTO media_preferences(publication_id,preference_key,data) VALUES (?,?,?)',
                (publication_id,preference_id,json.dumps(data)));return Response(status_code=204)
        row=db.one('SELECT data FROM media_preferences WHERE publication_id=? AND preference_key=?',(publication_id,preference_id))
        return json.loads(row['data']) if row else {'Id':preference_id,'SortBy':'SortName','SortOrder':'Ascending','ViewType':'Poster','CustomPrefs':{},'RememberIndexing':False,'RememberSorting':True}

    @router.post('/Sessions/Logout')
    def logout(publication_id:str,request:Request):
        authenticated(publication_id,request);db.execute('DELETE FROM media_sessions WHERE secret_hash=?',(digest(auth_token(request)),));return Response(status_code=204)

    @router.post('/Sessions/Capabilities/Full')
    @router.post('/Sessions/Capabilities')
    def capabilities(publication_id:str,request:Request):authenticated(publication_id,request);return Response(status_code=204)

    @router.get('/Sessions')
    def sessions(publication_id:str,request:Request):authenticated(publication_id,request);return []

    @router.get('/Branding/Configuration')
    def branding(publication_id:str,request:Request):authenticated(publication_id,request);return {'LoginDisclaimer':'','CustomCss':''}

    # Specific routes such as /Items/Filters must precede the /Items/{item_id} route.
    router.routes.sort(key=lambda route:route.path.count('{'))
    app.include_router(router)
