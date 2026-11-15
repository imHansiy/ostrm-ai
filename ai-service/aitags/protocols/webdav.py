"""Read-only AI metadata view; original OpenList /dav remains available unchanged."""
import base64
import posixpath
import xml.etree.ElementTree as ET
from datetime import datetime,timezone
from email.utils import format_datetime
from urllib.parse import quote
from fastapi import HTTPException,Request
from fastapi.responses import RedirectResponse,Response
from .formats import nfo
from .publisher import basename

DAV='DAV:'
ET.register_namespace('d',DAV)


def install_webdav(app,catalog):
    @app.api_route('/media/dav/{publication_id}',methods=['PROPFIND','OPTIONS','GET','HEAD','PUT','DELETE','MKCOL','MOVE','COPY','LOCK','UNLOCK'])
    @app.api_route('/media/dav/{publication_id}/{path:path}',methods=['PROPFIND','OPTIONS','GET','HEAD','PUT','DELETE','MKCOL','MOVE','COPY','LOCK','UNLOCK'])
    def dav(publication_id:str,request:Request,path:str=''):
        if request.method=='OPTIONS':return Response(status_code=204,headers={'DAV':'1','Allow':'OPTIONS,PROPFIND,GET,HEAD'})
        header=request.headers.get('authorization','')
        try:
            if not header.lower().startswith('basic '):raise ValueError('basic auth required')
            user,password=base64.b64decode(header[6:],validate=True).decode('utf-8').split(':',1)
            if user!=publication_id:raise ValueError('invalid user')
        except (ValueError,UnicodeError):raise HTTPException(401,'需要订阅账户',headers={'WWW-Authenticate':'Basic realm="OStrm AI", charset="UTF-8"'}) from None
        publication=catalog.store.authenticate(publication_id,password)
        if request.method not in ('GET','HEAD','PROPFIND'):raise HTTPException(405,'此 WebDAV 视图为只读',headers={'Allow':'OPTIONS,PROPFIND,GET,HEAD'})
        parts=[part for part in path.strip('/').split('/') if part]
        if len(parts)>2 or any(part in ('.','..') or '\\' in part for part in parts):raise HTTPException(404,'目录不存在')
        root=publication['public_base_url']+'/media/dav/'+publication_id+'/'
        item=catalog.item(publication,parts[0]) if parts else None
        file_rows={}
        if item:
            stem=basename(item['title']);file_rows[stem+'.'+(item['extension'] or 'mp4')]=('video',item['size'],item['mime'])
            metadata=nfo(item);file_rows[stem+'.nfo']=(metadata,len(metadata),'application/xml')
            file_rows['movie.nfo']=(metadata,len(metadata),'application/xml')
            jpeg=catalog.jpeg(item,False)
            if jpeg:file_rows['poster.jpg']=(jpeg,len(jpeg),'image/jpeg')
            subtitle=catalog.subtitle(item)
            if subtitle:file_rows[stem+'.zh.srt']=(subtitle,len(subtitle),'application/x-subrip')
            if len(parts)==2 and parts[1] not in file_rows:raise HTTPException(404,'文件不存在')
        if request.method in ('GET','HEAD'):
            if len(parts)<2:return Response('OStrm AI read-only media collection',media_type='text/plain')
            content,length,mime=file_rows[parts[1]]
            if content=='video':return RedirectResponse(catalog.playback_url(item),status_code=307)
            return Response(content if request.method=='GET' else b'',media_type=mime,headers={'Content-Length':str(length),'Accept-Ranges':'none'})
        depth=request.headers.get('depth','1')
        if depth not in ('0','1'):raise HTTPException(403,'支持 Depth: 0 或 1')
        multistatus=ET.Element(f'{{{DAV}}}multistatus')
        def entry(href,name,is_dir,length=0,mime='httpd/unix-directory',updated=0):
            response=ET.SubElement(multistatus,f'{{{DAV}}}response')
            ET.SubElement(response,f'{{{DAV}}}href').text=href
            propstat=ET.SubElement(response,f'{{{DAV}}}propstat');props=ET.SubElement(propstat,f'{{{DAV}}}prop')
            ET.SubElement(props,f'{{{DAV}}}displayname').text=name
            resource=ET.SubElement(props,f'{{{DAV}}}resourcetype')
            if is_dir:ET.SubElement(resource,f'{{{DAV}}}collection')
            ET.SubElement(props,f'{{{DAV}}}getcontentlength').text=str(length)
            ET.SubElement(props,f'{{{DAV}}}getcontenttype').text=mime
            stamp=datetime.fromtimestamp(updated or publication['updated'],timezone.utc)
            ET.SubElement(props,f'{{{DAV}}}getlastmodified').text=format_datetime(stamp,usegmt=True)
            ET.SubElement(props,f'{{{DAV}}}creationdate').text=stamp.isoformat().replace('+00:00','Z')
            ET.SubElement(props,f'{{{DAV}}}getetag').text='"'+str(round(stamp.timestamp()*1000))+'-'+str(length)+'"'
            ET.SubElement(propstat,f'{{{DAV}}}status').text='HTTP/1.1 200 OK'
        if not parts:
            entry(root,publication['name'],True)
            if depth=='1':
                for child in catalog.items(publication):entry(root+child['id']+'/',child['title'],True,updated=child['updated'])
        elif len(parts)==1:
            location=root+item['id']+'/'
            entry(location,item['title'],True,updated=item['updated'])
            if depth=='1':
                for name,(_,length,mime) in file_rows.items():entry(location+quote(name,safe=''),name,False,length,mime,item['updated'])
        else:
            _,length,mime=file_rows[parts[1]];entry(root+item['id']+'/'+quote(parts[1],safe=''),parts[1],False,length,mime,item['updated'])
        return Response(ET.tostring(multistatus,encoding='utf-8',xml_declaration=True),status_code=207,media_type='application/xml; charset=utf-8',headers={'DAV':'1'})
