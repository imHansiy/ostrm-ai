import io
import json
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import parse_qs,urlsplit
from fastapi.testclient import TestClient
from PIL import Image
from aitags.app import create_app
from aitags.covers import save_cover
from aitags.protocols.catalog import MediaCatalog
from aitags.protocols.store import Publication
from aitags.worker import fingerprint
from .conftest import add_video

ADMIN={'Authorization':'admin'}


def test_long_chinese_filenames_and_complete_backup(env,tmp_path):
    from aitags.protocols.publisher import basename
    from aitags.storage import copy_database
    from aitags.db import Database
    publication=setup(env);db,ol,worker,client=env
    publisher=client.app.state.publisher
    stem=basename('很长的中文电影标题'*80)+' [12345678]'
    directory=publisher.root/stem
    result=publisher.write(directory,{stem+'.ai.json':b'{}',stem+'.nfo':b'<movie/>'})
    assert result['written']==2
    assert all(len(path.name.encode())<256 for path in directory.iterdir())
    copy=Database(tmp_path/'restored.sqlite3');counts=copy_database(db,copy)
    assert counts['media_publications']==1
    assert copy.one('SELECT * FROM media_publications')==db.one('SELECT * FROM media_publications')
    response=client.post('/ai/api/exports/backup',headers=ADMIN)
    assert response.status_code==200
    archive=zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.read('media-secret.key')==client.app.state.media.store.key
    assert archive.read('aitags.sqlite3').startswith(b'SQLite format 3')


def test_scan_preserves_subtitle_and_movie_options(env):
    client=env[3]
    result=client.post('/ai/api/scan',headers=ADMIN,json={'paths':['/public'],'use_subtitles':True,'content_type':'movie'})
    assert result.status_code==200
    row=env[0].one('SELECT * FROM runs WHERE id=?',(result.json()['ids'][0],))
    assert row['use_subtitles']==1 and row['content_type']=='movie'


def setup(env,**options):
    db,ol,worker,client=env
    add_video(db,worker,'/public/a.mp4',ol,['旅行','城市'])
    add_video(db,worker,'/secret/b.mp4',ol,['NSFW'])
    output=io.BytesIO();Image.new('RGB',(48,32),(40,100,160)).save(output,'JPEG')
    save_cover(db,'/public/a.mp4',fingerprint(ol.objects['/public/a.mp4']),None,output.getvalue())
    result=client.post('/ai/api/exports/publications',headers=ADMIN,json={
        'name':'测试媒体库','roots':['/public'],'public_base_url':'https://media.example.test',**options})
    assert result.status_code==200,result.text
    publication=result.json();return publication


def local(url):
    result=urlsplit(url);return result.path+('?' +result.query if result.query else '')


def test_export_formats_share_scopes_ids_and_real_metadata(env):
    publication=setup(env);db,ol,worker,client=env
    links=publication['links']
    playlist=client.get(local(links['m3u']))
    assert playlist.status_code==200 and playlist.text.startswith('#EXTM3U\n')
    assert 'a' in playlist.text and '/secret/' not in playlist.text
    root=ET.fromstring(client.get(local(links['rss'])).content)
    entries=root.findall('channel/item');assert len(entries)==1
    assert entries[0].find('description').text=='真实画面简介'
    stream=entries[0].find('enclosure').attrib['url']
    assert stream in playlist.text
    archive=zipfile.ZipFile(io.BytesIO(client.get(local(links['strm_nfo_zip'])).content))
    names=archive.namelist();assert any(name.endswith('.strm') for name in names)
    assert any(name.endswith('-poster.jpg') for name in names)
    nfo=ET.fromstring(archive.read(next(name for name in names if name.endswith('.nfo'))))
    assert nfo.findtext('plot')=='真实画面简介'
    assert [tag.text for tag in nfo.findall('tag')]==['旅行','城市']
    document=json.loads(archive.read(next(name for name in names if name.endswith('.ai.json'))))
    assert document['source_path']=='/public/a.mp4' and document['manual_tags']==[]
    assert 'raw_url' not in str(document) and 'api_key' not in str(document)
    assert archive.read(next(name for name in names if name.endswith('.strm'))).decode().strip()==stream


def test_cms_tvbox_and_search_use_valid_maccms_shapes(env):
    publication=setup(env);client=env[3]
    tvbox=client.get(local(publication['links']['tvbox'])).json()
    assert tvbox['sites'][0]['type']==1
    api=tvbox['sites'][0]['api']
    assert '?' not in api # Clients may append ?ac=... directly.
    result=client.get(local(api)+'?ac=videolist&wd=城市').json()
    assert result['code']==1 and result['total']==1 and result['class']
    item=result['list'][0]
    assert isinstance(item['vod_id'],int) and 0<item['vod_id']<2**53
    assert item['vod_play_from']=='ostrm_ai' and item['vod_play_url'].startswith('播放$https://')
    assert item['vod_actor']=='' # AI characters are not real cast credits.
    assert client.get(local(api)+'?ac=detail&ids='+str(item['vod_id'])).json()['list'][0]['vod_id']==item['vod_id']
    assert client.get(local(api)+'?ac=videolist&wd=秘密').json()['total']==0


def test_access_is_scoped_revocable_rotatable_and_not_an_admin_credential(env):
    publication=setup(env);client=env[3];links=publication['links']
    assert client.post('/ai/api/exports/publications',json={'name':'x','roots':['/']}).status_code==403
    assert client.get(local(links['m3u']).split('?')[0]).status_code==401
    assert client.get('/ai/api/exports/publications',headers={'Authorization':'Bearer '+links['jellyfin_password']}).status_code==403
    secret_index=env[3].app.state.media.store.index('/secret/b.mp4')
    secret_url=client.app.state.media.url(publication,'image',{'id':secret_index['id'],'path':'/secret/b.mp4'})
    assert client.get(local(secret_url)).status_code==404
    client.post('/ai/api/exports/publications/'+publication['id']+'/rotate',headers=ADMIN)
    assert client.get(local(links['m3u'])).status_code==401
    new=client.get('/ai/api/exports/publications/'+publication['id']+'/links',headers=ADMIN).json()
    assert client.get(local(new['m3u'])).status_code==200
    client.post('/ai/api/exports/publications/'+publication['id']+'/revoke',headers=ADMIN)
    assert client.get(local(new['m3u'])).status_code==401


def test_playback_resolves_a_current_url_and_checks_file_version(env,monkeypatch):
    publication=setup(env);client=env[3];catalog=client.app.state.media
    item=catalog.items(publication)[0]
    monkeypatch.setattr('aitags.protocols.catalog.approve_url',lambda *args:None)
    source=catalog.url(publication,'stream',item)
    first=client.get(local(source),follow_redirects=False)
    assert first.status_code==307 and first.headers['location']==env[1].objects['/public/a.mp4']['raw_url']
    env[1].objects['/public/a.mp4']['raw_url']='https://media.example.test/fresh-token.mp4'
    assert client.head(local(source),follow_redirects=False).headers['location'].endswith('fresh-token.mp4')
    env[1].objects['/public/a.mp4']['size']=999
    assert client.get(local(source),follow_redirects=False).status_code==409


def login(client,publication):
    base=local(publication['links']['jellyfin_server'])
    result=client.post(base+'/Users/AuthenticateByName',json={'Username':publication['id'],'Pw':publication['links']['jellyfin_password']})
    assert result.status_code==200,result.text
    return base,result.json(),{'X-Emby-Token':result.json()['AccessToken']}


def test_jellyfin_auth_library_progress_favorites_and_restart(env):
    publication=setup(env);client=env[3]
    base,session,headers=login(client,publication);user=session['User']['Id']
    assert client.get(base+'/System/Info/Public').json()['Version']=='10.11.0'
    assert client.get(base+'/Items').status_code==401
    assert client.get(base+'/Users/'+user+'/Views',headers=headers).json()['Items'][0]['Id']=='library'
    result=client.get(base+'/Items',headers=headers).json()
    assert result['TotalRecordCount']==1
    item=result['Items'][0]
    assert item['Overview']=='真实画面简介' and item['Tags']==['旅行','城市']
    assert client.post(base+'/Sessions/Playing/Progress',headers=headers,json={'ItemId':item['Id'],'PositionTicks':20000000}).status_code==204
    assert client.post(base+'/Users/'+user+'/FavoriteItems/'+item['Id'],headers=headers).json()['IsFavorite']
    assert client.get(base+'/Items/Resume',headers=headers).json()['Items'][0]['UserData']['PlaybackPositionTicks']==20000000
    fresh=create_app(env[0],env[1],False)
    with TestClient(fresh) as restarted:
        restored=restarted.get(base+'/Items',headers=headers).json()['Items'][0]['UserData']
        assert restored['IsFavorite'] and restored['PlaybackPositionTicks']==20000000
        assert restarted.get(base+'/Users/wrong/Views',headers=headers).status_code==404
        restarted.post('/ai/api/exports/publications/'+publication['id']+'/revoke',headers=ADMIN)
        assert restarted.get(base+'/Items',headers=headers).status_code==401


def test_podcast_fields_cors_xml_escaping_and_manual_nfo_protection(env):
    publication=setup(env,author='作者',owner_email='owner@example.test');db,ol,worker,client=env
    db.execute('UPDATE videos SET result=? WHERE path=?',(json.dumps({'summary':'<故事>& 内容\x00','tags':['A"\n#EXTINF:-1,inject']}),'/public/a.mp4'))
    root=ET.fromstring(client.get(local(publication['links']['podcast'])).content)
    assert root.find('channel/{http://www.itunes.com/dtds/podcast-1.0.dtd}author').text=='作者'
    assert root.findtext('channel/item/description')=='<故事>& 内容'
    response=client.options(local(publication['links']['m3u']),headers={'Origin':'https://client.example','Access-Control-Request-Method':'GET'})
    assert response.status_code==204 and response.headers['access-control-allow-origin']=='*'
    publisher=client.app.state.publisher
    job=client.post('/ai/api/exports/publications/'+publication['id']+'/sync',headers=ADMIN).json()
    assert publisher.once()
    completed=db.one('SELECT * FROM media_export_jobs WHERE id=?',(job['id'],))
    assert completed['state']=='done'
    target=next(publisher.root.rglob('*.nfo'));target.write_text('<movie><title>人工编辑</title></movie>',encoding='utf-8')
    publisher.sync(publication)
    assert target.read_text(encoding='utf-8')=='<movie><title>人工编辑</title></movie>'


def test_readonly_webdav_exposes_real_videos_with_metadata_and_basic_auth(env):
    publication=setup(env);client=env[3]
    base=local(publication['links']['webdav_ai_server'])
    credentials=(publication['id'],publication['links']['webdav_password'])
    assert client.request('PROPFIND',base,headers={'Depth':'1'}).status_code==401
    response=client.request('PROPFIND',base,headers={'Depth':'1'},auth=credentials)
    assert response.status_code==207
    root=ET.fromstring(response.content)
    entries=root.findall('{DAV:}response');assert len(entries)==2
    location=local(entries[1].findtext('{DAV:}href'))
    listing=client.request('PROPFIND',location,headers={'Depth':'1'},auth=credentials)
    assert listing.status_code==207 and '.mp4' in listing.text and '.nfo' in listing.text and 'poster.jpg' in listing.text
    assert '/secret' not in listing.text
    assert client.request('PROPFIND',base,headers={'Depth':'infinity'},auth=credentials).status_code==403
    assert client.put(location+'bad.txt',content=b'bad',auth=credentials).status_code==405
    nfo_url=next(local(node.text) for node in ET.fromstring(listing.content).iter('{DAV:}href') if node.text.endswith('.nfo'))
    assert ET.fromstring(client.get(nfo_url,auth=credentials).content).findtext('plot')=='真实画面简介'


def test_upstream_metadata_merge_keeps_verified_cast_and_updates_only_ai_tags(env):
    publication=setup(env);db,ol,worker,client=env;publisher=client.app.state.publisher
    directory=publisher.directory('upstream');directory.mkdir()
    target=directory/'a.nfo'
    target.write_text('<movie><title>已核对的片名</title><plot>官方简介</plot><actor><name>真实演员</name></actor><tag>人工分类</tag></movie>',encoding='utf-8')
    item=client.app.state.media.items(publication)[0]
    publisher.write(directory,publisher.files(item,'a'))
    original=ET.fromstring(target.read_bytes())
    assert original.findtext('title')=='已核对的片名' and original.findtext('actor/name')=='真实演员'
    assert original.findtext('plot').startswith('官方简介')
    assert {tag.text for tag in original.findall('tag')}=={'人工分类','旅行','城市'}
    db.execute('UPDATE videos SET result=?,updated=updated+1 WHERE path=?',(json.dumps({'summary':'新的画面简介','tags':['美食']}),'/public/a.mp4'))
    client.app.state.media.invalidate();item=client.app.state.media.items(publication)[0]
    publisher.write(directory,publisher.files(item,'a'))
    updated=ET.fromstring(target.read_bytes())
    assert updated.findtext('title')=='已核对的片名' and updated.findtext('actor/name')=='真实演员'
    assert updated.findtext('plot')=='官方简介\n\n[OStrm AI 视频内容]\n新的画面简介'
    assert {tag.text for tag in updated.findall('tag')}=={'人工分类','美食'}


def test_missing_podcast_fields_and_malformed_jellyfin_requests_fail_cleanly(env):
    publication=setup(env);client=env[3]
    assert client.get(local(publication['links']['podcast'])).status_code==400
    base=local(publication['links']['jellyfin_server'])
    assert client.post(base+'/Users/AuthenticateByName',content=b'not json').status_code==400
    assert client.post(base+'/Users/AuthenticateByName',json=[]).status_code==400
    assert client.get(local(publication['links']['rss'])).headers['content-type'].startswith('application/rss+xml')
    head=client.head(local(publication['links']['rss']))
    assert head.status_code==200 and head.content==b'' and int(head.headers['content-length'])>0
