"""Exercise real Java JWT -> AI worker -> ffmpeg -> scoped external protocols."""
import io
import json
import os
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
import httpx

APP=os.getenv('STACK_URL','http://127.0.0.1:18080')
AI=os.getenv('AI_URL','http://127.0.0.1:18787')
MOCK=os.getenv('MOCK_URL','http://127.0.0.1:18789')
SOURCE=os.getenv('MOCK_SOURCE_URL',MOCK)
PUBLIC=os.getenv('MEDIA_TEST_URL',AI)
client=httpx.Client(timeout=40)

def wait(fn,seconds=150):
    end=time.monotonic()+seconds;last=None
    while time.monotonic()<end:
        try:
            result=fn()
            if result:return result
        except (httpx.HTTPError,KeyError,ValueError) as error:last=type(error).__name__
        time.sleep(1)
    raise AssertionError('Timed out waiting for stack: '+str(last))

def main():
    wait(lambda:client.get(APP+'/api/auth/check-user').status_code in (200,404))
    wait(lambda:client.get(MOCK+'/test/stats').status_code==200)
    wait(lambda:client.get(AI+'/ai/health').status_code==200)
    existing=client.get(APP+'/api/auth/check-user').json()['data']
    if not existing.get('hasUser') and not existing.get('exists'):
        registered=client.post(APP+'/api/auth/sign-up',json={'username':'protocol-test','password':'Fixture-Only-2026!'})
        assert registered.status_code==201,registered.text
    response=client.post(APP+'/api/auth/sign-in',json={'username':'protocol-test','password':'Fixture-Only-2026!'})
    response.raise_for_status();token=response.json()['data']['token'];headers={'Authorization':'Bearer '+token}
    def api(method,path,**kwargs):
        result=client.request(method,APP+'/api/ai'+path,headers=headers,**kwargs)
        assert result.is_success,(path,result.status_code,result.text[:500])
        return result.json()
    assert client.get(APP+'/api/ai/exports/publications').status_code in (401,403)
    settings=api('GET','/settings')['settings']
    settings.update(roots=['/videos'],model_url=SOURCE+'/v1',model_name='fixture-video',media_timeout=120,model_timeout=30,concurrency=1)
    api('PUT','/settings',json=settings)
    api('PUT','/model/key',json={'key':'fixture-only'})
    api('PUT','/library/covers',json={'enabled':True})
    scan=api('POST','/scan',json={'paths':['/videos'],'content_type':'auto','use_subtitles':False})
    def completed():
        result=api('POST','/library',json={'status':'done'})
        return result if result.get('items') else None
    library=wait(completed,240)
    assert library['items'][0]['summary'].startswith('蓝色画面'),library
    wait(lambda:completed()['items'][0]['cover'],90)
    stats=client.get(MOCK+'/test/stats').json()
    assert stats['model_requests']>=1 and stats['model_video_bytes']>1000,stats
    profile=api('POST','/exports/publications',json={'name':'协议联调媒体库','roots':['/videos'],
        'public_base_url':PUBLIC,'author':'OStrm AI','owner_email':'fixture@example.test'})
    links=profile['links']
    for name in ('rss','m3u','tvbox','libretv'):
        result=client.get(links[name]);result.raise_for_status()
    rss=ET.fromstring(client.get(links['rss']).content)
    stream=rss.find('channel/item/enclosure').attrib['url']
    ranged=client.get(stream,headers={'Range':'bytes=0-127'},follow_redirects=True)
    assert ranged.status_code==206 and len(ranged.content)==128
    subprocess.run(['ffmpeg','-v','error','-i',stream,'-t','1','-f','null','-'],check=True,timeout=30)
    archive=zipfile.ZipFile(io.BytesIO(client.get(links['strm_nfo_zip']).content))
    assert any(name.endswith('.nfo') for name in archive.namelist())
    assert any(name.endswith('-poster.jpg') for name in archive.namelist())
    cms=client.get(links['maccms'],params={'ac':'detail'}).json();assert cms['total']==1
    base=links['jellyfin_server']
    result=client.post(base+'/Users/AuthenticateByName',json={'Username':links['jellyfin_username'],'Pw':links['jellyfin_password']})
    result.raise_for_status();session=result.json();auth={'X-Emby-Token':session['AccessToken']}
    items=client.get(base+'/Items',headers=auth).json();item=items['Items'][0]
    assert item['Overview'].startswith('蓝色画面')
    playback=client.post(base+'/Items/'+item['Id']+'/PlaybackInfo',headers=auth,json={'UserId':session['User']['Id']})
    playback.raise_for_status();assert playback.json()['MediaSources'][0]['SupportsDirectPlay']
    progress=client.post(base+'/Sessions/Playing/Progress',headers=auth,json={'ItemId':item['Id'],'PositionTicks':10000000})
    assert progress.status_code==204
    job=api('POST','/exports/publications/'+profile['id']+'/sync')
    wait(lambda:any(row['id']==job['id'] and row['state']=='done' for row in api('GET','/exports/jobs')))
    # SDK/client checks can consume this private local fixture file. Never commit it.
    output=os.getenv('CLIENT_FIXTURE')
    if output:Path(output).write_text(json.dumps({'app':APP,'token':token,'links':links,'profile':profile['id']}),encoding='utf-8')
    print('PASS: Java JWT bridge, real MP4 compression, model fixture payload, STRM/NFO/M3U/RSS/CMS, Range/ffmpeg, Jellyfin progress, export queue')

if __name__=='__main__':main()
