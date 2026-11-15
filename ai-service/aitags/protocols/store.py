"""Durable, revocable publication scopes. No OpenList credentials leave the server."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from ..openlist import clean_path


class Publication(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name: str=Field(min_length=1,max_length=150)
    roots: list[str]=Field(min_length=1,max_length=100)
    tags: list[str]=Field(default_factory=list,max_length=30)
    content: Literal['all','general','adult']='all'
    public_base_url: str=Field(default='',max_length=1000)
    expires: float=Field(default=0,ge=0,allow_inf_nan=False)
    max_items: int=Field(default=5000,ge=1,le=50000)
    description: str=Field(default='',max_length=4000)
    author: str=Field(default='',max_length=200)
    owner_email: str=Field(default='',max_length=254)
    language: str=Field(default='zh-CN',max_length=30)
    sync_enabled: bool=False
    @field_validator('name','description','author','owner_email','language')
    @classmethod
    def valid_xml(cls,value):
        if any(ord(c)<32 and c not in '\t\n\r' for c in value):raise ValueError('字段包含无效控制字符')
        return value
    @field_validator('roots')
    @classmethod
    def paths(cls,values):
        return list(dict.fromkeys(clean_path(value).rstrip('/') or '/' for value in values))
    @field_validator('public_base_url')
    @classmethod
    def base_url(cls,value):
        if not value:return ''
        url=urlsplit(value)
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('公开地址需要不含账户、查询参数和片段的 HTTP(S) 地址')
        if any(character in value for character in '\r\n\t'):raise ValueError('公开地址无效')
        return value.rstrip('/')


SCHEMA=(
    """CREATE TABLE IF NOT EXISTS media_publications (
      id VARCHAR(32) PRIMARY KEY,data LONGTEXT NOT NULL,salt VARCHAR(64) NOT NULL,
      secret_hash VARCHAR(64) NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,
      created DOUBLE NOT NULL,updated DOUBLE NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS media_index (
      id VARCHAR(64) PRIMARY KEY,vod_id BIGINT NOT NULL UNIQUE,path TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS media_sessions (
      secret_hash VARCHAR(64) PRIMARY KEY,publication_id VARCHAR(32) NOT NULL,
      expires DOUBLE NOT NULL,device_id VARCHAR(256) NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS media_user_state (
      publication_id VARCHAR(32) NOT NULL,item_id VARCHAR(64) NOT NULL,
      position_ticks BIGINT NOT NULL DEFAULT 0,played INTEGER NOT NULL DEFAULT 0,
      favorite INTEGER NOT NULL DEFAULT 0,play_count INTEGER NOT NULL DEFAULT 0,
      last_played DOUBLE NOT NULL DEFAULT 0,PRIMARY KEY(publication_id,item_id))""",
    """CREATE TABLE IF NOT EXISTS media_targets (
      id VARCHAR(64) PRIMARY KEY,item_id VARCHAR(64) NOT NULL,directory TEXT NOT NULL,
      basename VARCHAR(240) NOT NULL,updated DOUBLE NOT NULL DEFAULT 0,error TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS media_export_jobs (
      id VARCHAR(32) PRIMARY KEY,publication_id VARCHAR(32) NOT NULL,
      state VARCHAR(20) NOT NULL,result LONGTEXT NOT NULL,created DOUBLE NOT NULL,updated DOUBLE NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS media_probes (
      id VARCHAR(64) PRIMARY KEY,fingerprint VARCHAR(64) NOT NULL,data LONGTEXT NOT NULL,updated DOUBLE NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS media_preferences (
      publication_id VARCHAR(32) NOT NULL,preference_key VARCHAR(200) NOT NULL,data LONGTEXT NOT NULL,
      PRIMARY KEY(publication_id,preference_key))""",
)


def digest(secret):return hashlib.sha256(secret.encode()).hexdigest()


class MediaStore:
    def __init__(self,db):
        self.db=db
        self.lock=threading.RLock()
        for ddl in SCHEMA:db.execute(ddl)
        self.secret_file=Path(db.path).parent/'media-secret.key'
        self.secret_file.parent.mkdir(parents=True,exist_ok=True)
        try:
            fd=os.open(self.secret_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as out:out.write(secrets.token_bytes(32))
        except FileExistsError:pass
        self.key=self.secret_file.read_bytes()
        if len(self.key)!=32:raise RuntimeError('media-secret.key 格式错误，请从备份恢复')

    def token(self,row):
        return base64.urlsafe_b64encode(hmac.new(self.key,(row['id']+':'+row['salt']).encode(),hashlib.sha256).digest()).decode().rstrip('=')

    def unpack(self,row):
        return {**json.loads(row['data']),'id':row['id'],'revoked':bool(row['revoked']),
                'created':row['created'],'updated':row['updated']}

    def create(self,body,base_url):
        now=time.time();identifier=secrets.token_hex(16)
        data=body.model_dump();data['public_base_url']=Publication.base_url(data['public_base_url'] or os.getenv('MEDIA_PUBLIC_BASE_URL','') or base_url)
        row={'id':identifier,'salt':secrets.token_hex(16)};token=self.token(row)
        self.db.execute('INSERT INTO media_publications(id,data,salt,secret_hash,created,updated) VALUES (?,?,?,?,?,?)',
            (identifier,json.dumps(data,ensure_ascii=False),row['salt'],digest(token),now,now))
        return self.get(identifier)

    def get(self,identifier,active=False):
        if len(identifier)>64:raise HTTPException(404,'订阅不存在')
        row=self.db.one('SELECT * FROM media_publications WHERE id=?',(identifier,))
        if not row:raise HTTPException(404,'订阅不存在')
        data=self.unpack(row)
        if active and (data['revoked'] or (data['expires'] and data['expires']<=time.time())):
            raise HTTPException(401,'订阅已撤销或过期')
        return data

    def authenticate(self,identifier,key):
        data=self.get(identifier,True)
        row=self.db.one('SELECT secret_hash FROM media_publications WHERE id=?',(identifier,))
        if not key or not hmac.compare_digest(row['secret_hash'],digest(key)):
            raise HTTPException(401,'订阅凭据无效')
        return data

    def secret(self,identifier):
        row=self.db.one('SELECT * FROM media_publications WHERE id=?',(identifier,))
        if not row:raise HTTPException(404,'订阅不存在')
        return self.token(row)

    def revoke(self,identifier):
        self.get(identifier)
        with self.db.connect() as connection:
            connection.execute('UPDATE media_publications SET revoked=1,updated=? WHERE id=?',(time.time(),identifier))
            connection.execute('DELETE FROM media_sessions WHERE publication_id=?',(identifier,))

    def rotate(self,identifier):
        self.get(identifier);row={'id':identifier,'salt':secrets.token_hex(16)}
        with self.db.connect() as connection:
            connection.execute('UPDATE media_publications SET salt=?,secret_hash=?,revoked=0,updated=? WHERE id=?',
                (row['salt'],digest(self.token(row)),time.time(),identifier))
            connection.execute('DELETE FROM media_sessions WHERE publication_id=?',(identifier,))
        return self.get(identifier)

    def index(self,path):
        identifier=digest(path)
        old=self.db.one('SELECT * FROM media_index WHERE id=?',(identifier,))
        if old:return old
        with self.lock:
            for counter in range(16):
                number=int(digest(path+':'+str(counter))[:13],16)
                existing=self.db.one('SELECT * FROM media_index WHERE vod_id=?',(number,))
                if existing and existing['id']!=identifier:continue
                self.db.execute('REPLACE INTO media_index(id,vod_id,path) VALUES (?,?,?)',(identifier,number,path))
                return {'id':identifier,'vod_id':number,'path':path}
        raise RuntimeError('无法分配媒体编号')

    def index_many(self,paths):
        # One read and one batched write, including on remote TiDB.
        with self.lock:
            rows=self.db.all('SELECT * FROM media_index')
            by_id={row['id']:row for row in rows};numbers={row['vod_id']:row['id'] for row in rows};pending=[]
            result={}
            for path in paths:
                identifier=digest(path)
                if identifier not in by_id:
                    for counter in range(16):
                        number=int(digest(path+':'+str(counter))[:13],16)
                        if number not in numbers or numbers[number]==identifier:break
                    else:raise RuntimeError('无法分配媒体编号')
                    row={'id':identifier,'vod_id':number,'path':path}
                    by_id[identifier]=row;numbers[number]=identifier;pending.append((identifier,number,path))
                result[path]=by_id[identifier]
            if pending:
                with self.db.connect() as connection:
                    connection.executemany('REPLACE INTO media_index(id,vod_id,path) VALUES (?,?,?)',pending)
            return result

    def session(self,publication,device=''):
        token=secrets.token_urlsafe(32)
        self.db.execute('INSERT INTO media_sessions(secret_hash,publication_id,expires,device_id) VALUES (?,?,?,?)',
            (digest(token),publication['id'],time.time()+30*86400,device[:256]))
        return token

    def authenticate_session(self,identifier,token):
        publication=self.get(identifier,True)
        row=self.db.one('SELECT * FROM media_sessions WHERE secret_hash=? AND publication_id=?',(digest(token),identifier)) if token else None
        if not row or row['expires']<=time.time():raise HTTPException(401,'登录凭据无效或过期')
        return publication

    def user_state(self,identifier,item):
        return self.db.one('SELECT * FROM media_user_state WHERE publication_id=? AND item_id=?',(identifier,item)) or {
            'position_ticks':0,'played':0,'favorite':0,'play_count':0,'last_played':0}

    def update_state(self,identifier,item,**changes):
        with self.lock:
            data={**self.user_state(identifier,item),**changes}
            self.db.execute('REPLACE INTO media_user_state(publication_id,item_id,position_ticks,played,favorite,play_count,last_played) VALUES (?,?,?,?,?,?,?)',
                (identifier,item,max(0,int(data['position_ticks'])),int(bool(data['played'])),int(bool(data['favorite'])),max(0,int(data['play_count'])),float(data['last_played'])))
        return data
