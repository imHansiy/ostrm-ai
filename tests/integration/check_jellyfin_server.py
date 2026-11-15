"""Import generated STRM/NFO/poster into an actual Jellyfin 10.11 server."""
import os
import time
import httpx
base=os.getenv('REAL_JELLYFIN_URL','http://127.0.0.1:8096')
client=httpx.Client(timeout=40)
def wait(fn):
    for _ in range(120):
        try:
            value=fn()
            if value:return value
        except (httpx.HTTPError,KeyError,ValueError):pass
        time.sleep(1)
    raise AssertionError('Real Jellyfin did not become ready or import generated metadata')
def request(method,path,**kwargs):
    response=client.request(method,base+path,**kwargs);response.raise_for_status()
    return response
wait(lambda:client.get(base+'/System/Info/Public').status_code==200)
request('POST','/Startup/Configuration',json={'UICulture':'en-US','MetadataCountryCode':'CN','PreferredMetadataLanguage':'zh'})
request('POST','/Startup/User',json={'Name':'protocol-test','Password':'Fixture-Only-2026!'})
request('POST','/Startup/RemoteAccess',json={'EnableRemoteAccess':True,'EnableAutomaticPortMapping':False})
request('POST','/Startup/Complete')
login=request('POST','/Users/AuthenticateByName',headers={'Authorization':'MediaBrowser Client="OStrmTest", Device="CI", DeviceId="ci", Version="0.2.0"'},json={'Username':'protocol-test','Pw':'Fixture-Only-2026!'}).json()
client.headers['X-Emby-Token']=login['AccessToken']
request('POST','/Library/VirtualFolders',params={'name':'AI','collectionType':'movies','refreshLibrary':'true'},
    json={'LibraryOptions':{'PathInfos':[{'Path':'/media-ai'}],'EnableInternetProviders':False,'EnableRealtimeMonitor':False}})
request('POST','/Library/Refresh')
def imported():
    result=request('GET','/Items',params={'recursive':'true','includeItemTypes':'Movie','fields':'Overview,Tags,Path'}).json()
    return next((item for item in result['Items'] if '蓝色画面' in item.get('Overview','')),None)
item=wait(imported)
assert '蓝色' in item['Tags'] and item['Path'].endswith('.strm'),item
assert item['ImageTags'].get('Primary'),item
print('PASS: real Jellyfin imports generated STRM, NFO overview/tags, and poster image')
