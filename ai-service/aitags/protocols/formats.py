"""Standards-oriented serializers. Model strings are data, never markup."""
import html
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime,timezone
from email.utils import format_datetime
from urllib.parse import urlencode
from .catalog import line

MEDIA='http://search.yahoo.com/mrss/'
ITUNES='http://www.itunes.com/dtds/podcast-1.0.dtd'
ATOM='http://www.w3.org/2005/Atom'
ET.register_namespace('media',MEDIA);ET.register_namespace('itunes',ITUNES);ET.register_namespace('atom',ATOM)


def element(parent,key,value):
    child=ET.SubElement(parent,key)
    child.text=re.sub(r'[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]','',str(value))
    return child


def xml(root):return ET.tostring(root,encoding='utf-8',xml_declaration=True)


def nfo(item,existing=None):
    if existing:
        if len(existing)>2*1024*1024 or b'<!DOCTYPE' in existing.upper() or b'<!ENTITY' in existing.upper():
            raise ValueError('NFO 包含不受支持的 XML 声明')
        root=ET.fromstring(existing)
        if root.tag not in ('movie','episodedetails','musicvideo'):raise ValueError('不受支持的 NFO 类型')
    else:
        episode=re.search(r'(?i)\bS(\d{1,3})E(\d{1,4})\b',item['title'])
        root=ET.Element('episodedetails' if episode else 'movie')
        element(root,'title',item['title'])
        if item['year']!='unknown':element(root,'year',item['year'])
        if episode:element(root,'season',int(episode[1]));element(root,'episode',int(episode[2]))
    plot=root.find('plot')
    if plot is None:plot=element(root,'plot',item['summary'])
    else:
        # Preserve provider/manual synopsis, and replace only our marked addition.
        base=(plot.text or '').split('\n\n[OStrm AI 视频内容]\n')[0]
        plot.text=base+('\n\n[OStrm AI 视频内容]\n'+item['summary'] if base!=item['summary'] else '')
    for field,value in [('outline',item['summary'][:200]),('runtime',math.ceil(item['duration']/60))]:
        if root.find(field) is None:element(root,field,value)
    tags={entry.text for entry in root.findall('tag')}
    for value in item['tags']:
        if value not in tags:element(root,'tag',value)
    genres={entry.text for entry in root.findall('genre')}
    for value in item['categories']:
        if value not in genres:element(root,'genre',value)
    if root.find('uniqueid[@type="ostrm-ai"]') is None:
        child=element(root,'uniqueid',item['id']);child.set('type','ostrm-ai');child.set('default','false')
    # Fictional characters from AI are not verified cast: keep them in .ai.json.
    return xml(root)


def ai_document(catalog,item):
    return json.dumps({'schema':'ostrm-ai.media/1','id':item['id'],'source_path':item['path'],
        'fingerprint':item['fingerprint'],'title':item['title'],'year':item['year'],
        'year_source':'filename' if item['year']!='unknown' else None,
        'tags':item['tags'],'ai_tags':item['ai_tags'],'manual_tags':item['manual_tags'],
        'adult':item['adult'],'result':catalog.full_result(item)},ensure_ascii=False,indent=2).encode('utf-8')


def m3u(catalog,publication,items):
    def attribute(value):return line(value).replace('"',"'")
    lines=['#EXTM3U']
    for item in items:
        lines.append(f'#EXTINF:{round(item["duration"]) or -1} tvg-id="{item["id"]}" tvg-name="{attribute(item["title"])}" tvg-logo="{catalog.url(publication,"image",item)}" group-title="{attribute(item["categories"][0])}",{line(item["title"])}')
        lines.append(catalog.url(publication,'stream',item))
    return ('\n'.join(lines)+'\n').encode('utf-8')


def rss(catalog,publication,items,podcast=False):
    if podcast:
        if not publication['author'] or '@' not in publication['owner_email']:
            raise ValueError('视频播客需要配置作者和联系人邮箱')
        items=[item for item in items if item['extension'] in ('mp4','m4v','mov')]
        if not items:raise ValueError('视频播客需要可用的 MP4、M4V 或 MOV 条目')
    root=ET.Element('rss',{'version':'2.0'});channel=ET.SubElement(root,'channel')
    element(channel,'title',publication['name']);element(channel,'link',publication['public_base_url'])
    element(channel,'description',publication['description'] or publication['name'])
    element(channel,'language',publication['language'])
    ET.SubElement(channel,f'{{{ATOM}}}link',{'href':catalog.url(publication,'podcast.xml' if podcast else 'rss.xml'),'rel':'self','type':'application/rss+xml'})
    if podcast:
        element(channel,f'{{{ITUNES}}}author',publication['author'])
        element(channel,f'{{{ITUNES}}}explicit','true' if any(item['adult'] for item in items) else 'false')
        element(channel,f'{{{ITUNES}}}type','episodic')
        owner=ET.SubElement(channel,f'{{{ITUNES}}}owner');element(owner,f'{{{ITUNES}}}name',publication['author']);element(owner,f'{{{ITUNES}}}email',publication['owner_email'])
        ET.SubElement(channel,f'{{{ITUNES}}}category',{'text':'TV & Film'})
        ET.SubElement(channel,f'{{{ITUNES}}}image',{'href':catalog.url(publication,'podcast-art.jpg')})
    for item in items:
        entry=ET.SubElement(channel,'item');element(entry,'title',item['title'])
        guid=element(entry,'guid','urn:ostrm-ai:'+item['id']);guid.set('isPermaLink','false')
        media=catalog.url(publication,'stream',item)
        element(entry,'link',media);element(entry,'description',item['summary'])
        try:date=datetime.fromtimestamp(item['date'] or item['updated'],timezone.utc)
        except (ValueError,OverflowError,OSError):date=datetime(1970,1,1,tzinfo=timezone.utc)
        element(entry,'pubDate',format_datetime(date,usegmt=True))
        ET.SubElement(entry,'enclosure',{'url':media,'length':str(item['size']),'type':item['mime']})
        content=ET.SubElement(entry,f'{{{MEDIA}}}content',{'url':media,'fileSize':str(item['size']),'type':item['mime'],'medium':'video','duration':str(round(item['duration']))})
        element(content,f'{{{MEDIA}}}title',item['title']);element(content,f'{{{MEDIA}}}description',item['summary'])
        element(content,f'{{{MEDIA}}}keywords',', '.join(item['tags']))
        ET.SubElement(content,f'{{{MEDIA}}}thumbnail',{'url':catalog.url(publication,'image',item)})
        for category in item['categories']:element(entry,'category',category);element(content,f'{{{MEDIA}}}category',category)
        if item['adult']:element(content,f'{{{MEDIA}}}rating','adult').set('scheme','urn:simple')
        if podcast:
            element(entry,f'{{{ITUNES}}}explicit','true' if item['adult'] else 'false')
            element(entry,f'{{{ITUNES}}}duration',round(item['duration']))
    return xml(root)


def cms_classes(items):
    from ..library import CATEGORY_TAGS
    labels=[*CATEGORY_TAGS,'其他']
    return [{'type_id':index+1,'type_name':label} for index,label in enumerate(labels) if any(label in item['categories'] for item in items)]


def cms_item(catalog,publication,item):
    from ..library import CATEGORY_TAGS
    labels=[*CATEGORY_TAGS,'其他'];category='成人内容' if item['adult'] else item['categories'][0]
    return {'vod_id':item['vod_id'],'vod_name':item['title'],'type_id':labels.index(category)+1,'type_name':category,
        'vod_pic':catalog.url(publication,'image',item),'vod_remarks':f'{round(item["duration"])} 秒',
        'vod_year':item['year'] if item['year']!='unknown' else '',
        'vod_class':','.join(item['categories']),'vod_tag':','.join(item['tags']),
        'vod_content':html.escape(item['summary']),'vod_actor':'','vod_director':'',
        'vod_play_from':'ostrm_ai','vod_play_server':'no','vod_play_note':'',
        'vod_play_url':'播放$'+catalog.url(publication,'stream',item)}


def tvbox(catalog,publication,split=False):
    if split and publication['content']=='all':
        api=catalog.url(publication,'api.php/provide/vod')
        return {'sites':[{'key':'ostrm_ai_'+publication['id']+'_'+scope,
            'name':publication['name']+' · '+name,'type':1,'api':api+'/'+scope,
            'searchable':1,'quickSearch':1,'filterable':0}
            for scope,name in [('general','非成人'),('adult','成人')]],'lives':[]}
    return {'sites':[{'key':'ostrm_ai_'+publication['id'],'name':publication['name'],'type':1,
        'api':catalog.url(publication,'api.php/provide/vod'),'searchable':1,'quickSearch':1,'filterable':0}],
        'lives':[{'name':publication['name'],'type':0,'url':catalog.url(publication,'playlist.m3u')}]}


def libretv(catalog,publication):
    adult=any(item['adult'] for item in catalog.items(publication))
    return {'version':2,'name':publication['name'],'sources':[{
        'name':publication['name'],'url':catalog.url(publication,'api.php/provide/vod'),'isAdult':adult}],
        'liveSources':[{'name':publication['name'],'url':catalog.url(publication,'playlist.m3u')}]}
