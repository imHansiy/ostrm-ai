"""Atomic sidecar publishing, restartable export jobs, and bounded ZIP downloads."""
import hashlib
import io
import json
import os
import re
import secrets
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from fastapi import HTTPException
from .formats import ai_document,nfo
from .store import digest


def basename(value):
    value=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',value).strip(' .')
    # Leave room for .ai.json, image suffixes, and atomic temporary names on Unix.
    value=value.encode('utf-8')[:180].decode('utf-8','ignore').strip(' .') or 'video'
    if value.upper().split('.')[0] in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}:value='_'+value
    return value


class Publisher:
    def __init__(self,catalog):
        self.catalog=catalog;self.db=catalog.db
        self.root=Path(os.getenv('MEDIA_EXPORT_DIR',str(Path(self.db.path).parent/'exports'))).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.stop=threading.Event();self.thread=None
        self.max_bytes=int(os.getenv('MEDIA_EXPORT_MAX_BYTES',str(256*1024**2)))

    def directory(self,relative):
        requested=Path(relative)
        if requested.is_absolute() or '..' in requested.parts or '\\' in relative:
            raise HTTPException(400,'导出目录必须是输出根目录内的相对路径')
        target=(self.root/requested).resolve()
        if not target.is_relative_to(self.root):raise HTTPException(400,'导出目录越界')
        return target

    def files(self,item,stem,publication=None):
        files={stem+'.nfo':nfo(item),stem+'.ai.json':ai_document(self.catalog,item)}
        image=self.catalog.jpeg(item,False)
        if image:files[stem+'-poster.jpg']=image
        subtitle=self.catalog.subtitle(item)
        if subtitle:files[stem+'.zh.srt']=subtitle
        if publication:files[stem+'.strm']=(self.catalog.url(publication,'stream',item)+'\n').encode('utf-8')
        return files

    def atomic(self,target,data):
        if target.is_symlink() or not target.resolve().is_relative_to(self.root):raise HTTPException(400,'输出文件路径越界')
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_name('.'+target.name+'.'+secrets.token_hex(4)+'.tmp')
        try:
            with temporary.open('xb') as out:out.write(data)
            temporary.replace(target)
        finally:temporary.unlink(missing_ok=True)

    def write(self,directory,files,merge_nfo=True):
        directory=self.directory(directory.relative_to(self.root).as_posix())
        directory.mkdir(parents=True,exist_ok=True)
        manifest_path=directory/'.ostrm-ai-manifest.json'
        if manifest_path.is_symlink():raise HTTPException(400,'资料清单不可使用符号链接')
        try:manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):manifest={}
        if not isinstance(manifest,dict):manifest={}
        native=manifest.setdefault('_native_nfo',[]);original_tag_sets=manifest.setdefault('_nfo_base_tags',{})
        report={'written':0,'unchanged':0,'conflicts':[],'removed':0}
        for name,data in files.items():
            if re.search(r'[<>:"/\\|?*\x00-\x1f]',name) or len(name.encode('utf-8'))>230 or name in ('.','..'):
                raise HTTPException(400,'导出文件名不合法')
            target=directory/name
            if target.is_symlink() or not target.resolve().is_relative_to(self.root):raise HTTPException(400,'输出文件路径越界')
            if target.exists():
                previous=target.read_bytes()
                if previous==data:report['unchanged']+=1;continue
                previous_hash=hashlib.sha256(previous).hexdigest()
                if name in manifest and manifest[name]!=previous_hash:
                    report['conflicts'].append(name);continue
                if name not in manifest and not (merge_nfo and name.endswith('.nfo')):
                    report['conflicts'].append(name);continue
                if merge_nfo and name.endswith('.nfo'):
                    # NFOs created by OStrm/TMDB keep their title, cast, and synopsis.
                    try:
                        import xml.etree.ElementTree as ET
                        generated=ET.fromstring(data)
                        if len(previous)>2*1024**2 or b'<!DOCTYPE' in previous.upper() or b'<!ENTITY' in previous.upper():raise ValueError('XML declarations')
                        original=ET.fromstring(previous)
                        if original.tag!=generated.tag:raise ValueError('NFO media type mismatch')
                        from .formats import xml,element
                        if name not in original_tag_sets:original_tag_sets[name]=[entry.text for entry in original.findall('tag')] if name not in native else []
                        original_tags=set(original_tag_sets[name])
                        for entry in list(original.findall('tag')):original.remove(entry)
                        for tag in original_tag_sets[name]:element(original,'tag',tag)
                        for tag in generated.findall('tag'):
                            if tag.text not in original_tags:element(original,'tag',tag.text)
                        plot=original.find('plot');new_plot=generated.findtext('plot','')
                        if plot is None:element(original,'plot',new_plot)
                        elif name in native:plot.text=new_plot
                        else:
                            base=(plot.text or '').split('\n\n[OStrm AI 视频内容]\n')[0]
                            plot.text=base+('\n\n[OStrm AI 视频内容]\n'+new_plot if new_plot and base!=new_plot else '')
                        data=xml(original)
                    except (ValueError,ET.ParseError):report['conflicts'].append(name);continue
            elif name.endswith('.nfo'):
                if name not in native:native.append(name)
                original_tag_sets[name]=[]
            self.atomic(target,data);manifest[name]=hashlib.sha256(data).hexdigest();report['written']+=1
        self.atomic(manifest_path,json.dumps(manifest,ensure_ascii=False,sort_keys=True).encode('utf-8'))
        return report

    def sync(self,publication):
        with self.lock:
            root=self.directory(publication['id']);items=self.catalog.items(publication)
            report={'items':len(items),'written':0,'unchanged':0,'conflicts':[],'missing_covers':0}
            total_bytes=0
            for offset in range(0,len(items),100):
                batch=items[offset:offset+100]
                with self.catalog.artifact_batch(batch):
                    for item in batch:
                        stem=basename(item['title'])[:110]+' ['+item['id'][:8]+']'
                        directory=root/stem
                        files=self.files(item,stem,publication)
                        total_bytes+=sum(len(data) for data in files.values())
                        if total_bytes>self.max_bytes:raise HTTPException(413,'导出资料超过上限，请拆分订阅目录')
                        if stem+'-poster.jpg' not in files:report['missing_covers']+=1
                        result=self.write(directory,files)
                        report['written']+=result['written'];report['unchanged']+=result['unchanged']
                        report['conflicts'].extend(stem+'/'+name for name in result['conflicts'])
            return report

    def zip(self,publication):
        output=tempfile.SpooledTemporaryFile(max_size=8*1024**2)
        try:
            with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                items=self.catalog.items(publication)
                for offset in range(0,len(items),100):
                    batch=items[offset:offset+100]
                    with self.catalog.artifact_batch(batch):
                        for item in batch:
                            stem=basename(item['title'])[:110]+' ['+item['id'][:8]+']'
                            for name,data in self.files(item,stem,publication).items():
                                archive.writestr(stem+'/'+name,data)
                                if output.tell()>self.max_bytes:raise HTTPException(413,'导出包超过上限，请拆分订阅目录')
                archive.writestr('README.txt','STRM/NFO/封面来自 OStrm AI。播放地址是限定目录的可撤销凭据，请仅导入受信任的播放器。\n')
            output.seek(0);return output
        except BaseException:output.close();raise

    def enqueue(self,publication):
        with self.lock:
            existing=self.db.one("SELECT * FROM media_export_jobs WHERE publication_id=? AND state IN ('queued','running')",(publication['id'],))
            if existing:return existing
            identifier=secrets.token_hex(16);now=time.time()
            self.db.execute('INSERT INTO media_export_jobs(id,publication_id,state,result,created,updated) VALUES (?,?,?,?,?,?)',
                            (identifier,publication['id'],'queued','{}',now,now))
            return self.db.one('SELECT * FROM media_export_jobs WHERE id=?',(identifier,))

    def once(self):
        job=self.db.one("SELECT * FROM media_export_jobs WHERE state='queued' ORDER BY created LIMIT 1")
        if not job:
            target=self.db.one("""SELECT v.path FROM media_targets t JOIN media_index i ON t.item_id=i.id
                JOIN videos v ON i.path=v.path WHERE v.status='done' AND v.updated>t.updated ORDER BY v.updated LIMIT 1""")
            if target:self.publish_path(target['path']);return True
            return False
        self.db.execute("UPDATE media_export_jobs SET state='running',updated=? WHERE id=?",(time.time(),job['id']))
        try:
            publication=self.catalog.store.get(job['publication_id'],True)
            result=self.sync(publication);state='done'
        except Exception as error:
            result={'error':'导出失败：'+type(error).__name__};state='failed'
        self.db.execute('UPDATE media_export_jobs SET state=?,result=?,updated=? WHERE id=?',
                        (state,json.dumps(result,ensure_ascii=False),time.time(),job['id']))
        return True

    def publish_path(self,path):
        item_index=self.catalog.store.index(path)
        row=self.db.one("SELECT * FROM videos WHERE path=? AND status='done'",(path,))
        if not row:return
        self.catalog.invalidate();public={**row,**self.db.public_video(row)};item=self.catalog.normalize(public,item_index)
        for target in self.db.all('SELECT * FROM media_targets WHERE item_id=?',(item['id'],)):
            try:
                with self.lock:result=self.write(self.directory(target['directory']),self.files(item,target['basename']))
                self.db.execute('UPDATE media_targets SET updated=?,error=? WHERE id=?',
                    (time.time(),json.dumps(result,ensure_ascii=False),target['id']))
            except Exception as error:
                self.db.execute('UPDATE media_targets SET updated=?,error=? WHERE id=?',(time.time(),'资料写入失败：'+type(error).__name__,target['id']))

    def start(self):
        self.db.execute("UPDATE media_export_jobs SET state='queued',updated=? WHERE state='running'",(time.time(),))
        def run():
            last_sync=0
            while not self.stop.is_set():
                try:
                    if self.once():continue
                    if time.monotonic()-last_sync>=60:
                        for row in self.db.all('SELECT * FROM media_publications WHERE revoked=0'):
                            publication=self.catalog.store.unpack(row)
                            if publication['sync_enabled'] and (not publication['expires'] or publication['expires']>time.time()):self.enqueue(publication)
                        last_sync=time.monotonic()
                except Exception:self.db.event('/','warning','协议资料导出暂时失败，将在下一轮重试')
                self.stop.wait(2)
        self.thread=threading.Thread(target=run,name='media-publisher',daemon=True);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=20)
