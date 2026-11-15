"""Restart both deployed containers and verify original credentials/data still work."""
import json
import subprocess
import time
from pathlib import Path
import httpx

fixture=json.loads(Path('.local/docker-stack.json').read_text(encoding='utf-8'))
client=httpx.Client(timeout=15)

def ready(url):
    for _ in range(90):
        try:
            if client.get(url).status_code==200:return
        except httpx.HTTPError:pass
        time.sleep(1)
    raise AssertionError('Container did not recover after restart')

subprocess.run(['docker','restart','app-container'],check=True,stdout=subprocess.DEVNULL)
ready(fixture['app']+'/api/auth/check-user')
headers={'Authorization':'Bearer '+fixture['token']}
result=client.get(fixture['app']+'/api/ai/exports/publications',headers=headers)
assert result.status_code==200 and any(row['id']==fixture['profile'] for row in result.json()['items'])
subprocess.run(['docker','restart','ai-container'],check=True,stdout=subprocess.DEVNULL)
ready('http://127.0.0.1:8787/ai/health')
assert client.get(fixture['links']['m3u']).status_code==200
links=fixture['links'];base=links['jellyfin_server']
auth=client.post(base+'/Users/AuthenticateByName',json={'Username':links['jellyfin_username'],'Pw':links['jellyfin_password']})
assert auth.status_code==200
item=client.get(base+'/Items',headers={'X-Emby-Token':auth.json()['AccessToken']}).json()['Items'][0]
assert item['Overview'].startswith('蓝色画面') and item['UserData']['PlaybackPositionTicks']==10000000
print('PASS: actual app/worker container restarts preserve JWT, subscription credentials, metadata, and Jellyfin watch state')
