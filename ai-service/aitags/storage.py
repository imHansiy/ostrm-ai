"""Optional TiDB storage with a private SQLite cover cache."""

import os
import re
import sqlite3
import threading
from contextlib import contextmanager

from .db import Database


TABLES = {
    'settings': 'key value',
    'videos': 'rowid path fingerprint identity size modified status result manual removed error updated',
    'covers': 'path fingerprint sidecar jpeg updated',
    'runs': 'id root state created error content_type use_subtitles',
    'directories': 'id run_id path page depth state',
    'tasks': 'id run_id path fingerprint state attempts error created',
    'history': 'id path created state message',
    'aliases': 'alias canonical',
    'favorites': 'tag',
}

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS settings (`key` VARCHAR(128) PRIMARY KEY,value LONGTEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS videos (
      rowid BIGINT PRIMARY KEY AUTO_INCREMENT,path TEXT NOT NULL,
      path_key BINARY(32) AS (UNHEX(SHA2(path,256))) STORED UNIQUE,
      fingerprint CHAR(64) NOT NULL,identity VARCHAR(512) NOT NULL DEFAULT '',
      size BIGINT NOT NULL,modified VARCHAR(128) NOT NULL,status VARCHAR(16) NOT NULL DEFAULT 'pending',
      result LONGTEXT NOT NULL,manual LONGTEXT NOT NULL,removed LONGTEXT NOT NULL,
      error VARCHAR(1000) NOT NULL DEFAULT '',updated DOUBLE NOT NULL,INDEX identity_idx(identity))""",
    """CREATE TABLE IF NOT EXISTS covers (
      path TEXT NOT NULL,fingerprint CHAR(64) NOT NULL,sidecar TEXT,jpeg BLOB,updated DOUBLE NOT NULL,
      path_key BINARY(32) AS (UNHEX(SHA2(path,256))) STORED UNIQUE)""",
    """CREATE TABLE IF NOT EXISTS runs (
      id BIGINT PRIMARY KEY AUTO_INCREMENT,root TEXT NOT NULL,state VARCHAR(16) NOT NULL DEFAULT 'running',
      created DOUBLE NOT NULL,error VARCHAR(1000) NOT NULL DEFAULT '',
      content_type VARCHAR(16) NOT NULL DEFAULT 'auto',use_subtitles TINYINT NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS directories (
      id BIGINT PRIMARY KEY AUTO_INCREMENT,run_id BIGINT NOT NULL,path TEXT NOT NULL,
      page INT NOT NULL DEFAULT 1,depth INT NOT NULL DEFAULT 0,state VARCHAR(16) NOT NULL DEFAULT 'queued',
      path_key BINARY(32) AS (UNHEX(SHA2(path,256))) STORED,
      UNIQUE KEY run_path(run_id,path_key),INDEX state_id(state,id))""",
    """CREATE TABLE IF NOT EXISTS tasks (
      id BIGINT PRIMARY KEY AUTO_INCREMENT,run_id BIGINT NOT NULL,path TEXT NOT NULL,fingerprint CHAR(64) NOT NULL,
      state VARCHAR(16) NOT NULL DEFAULT 'queued',attempts INT NOT NULL DEFAULT 0,error VARCHAR(1000) NOT NULL DEFAULT '',
      created DOUBLE NOT NULL,path_key BINARY(32) AS (UNHEX(SHA2(path,256))) STORED,
      live_key BINARY(32) AS (CASE WHEN state IN ('queued','running') THEN UNHEX(SHA2(path,256)) END) STORED UNIQUE,
      UNIQUE KEY run_path(run_id,path_key),INDEX state_id(state,id))""",
    """CREATE TABLE IF NOT EXISTS history (
      id BIGINT PRIMARY KEY AUTO_INCREMENT,path TEXT NOT NULL,created DOUBLE NOT NULL,
      state VARCHAR(16) NOT NULL,message VARCHAR(1000) NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS aliases (alias VARCHAR(40) PRIMARY KEY,canonical VARCHAR(40) NOT NULL)",
    "CREATE TABLE IF NOT EXISTS favorites (tag VARCHAR(40) PRIMARY KEY)",
)


def tidb_connection(database=None):
    import certifi
    import pymysql
    return pymysql.connect(
        host=os.environ['AI_TIDB_HOST'],port=int(os.getenv('AI_TIDB_PORT','4000')),
        user=os.environ['AI_TIDB_USER'],password=os.environ['AI_TIDB_PASSWORD'],
        database=database,charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor,
        ssl_ca=os.getenv('AI_TIDB_CA') or certifi.where(),ssl_verify_cert=True,ssl_verify_identity=True,
        connect_timeout=10,read_timeout=45,write_timeout=45,binary_prefix=True,
    )


class TiDBConnection:
    def __init__(self, connection):
        self.connection=connection

    def execute(self, sql, args=()):
        cursor=self.connection.cursor()
        cursor.execute(sql.replace('?','%s'),args or None)
        return cursor

    def executemany(self, sql, rows):
        cursor=self.connection.cursor()
        cursor.executemany(sql.replace('?','%s'),rows)
        return cursor


class TiDBDatabase(Database):
    backend='tidb'

    def __init__(self, path, create=False):
        self.path=str(path)
        self.lock=threading.RLock()
        self.idle=[]
        self.read_slots=threading.BoundedSemaphore(4)
        self.read_lock=threading.Lock()
        self.read_idle=[]
        self.name=os.getenv('AI_TIDB_DATABASE','openlist_ai')
        if not re.fullmatch(r'[A-Za-z0-9_]{1,64}',self.name):
            raise ValueError('Invalid AI_TIDB_DATABASE')
        if create:
            with tidb_connection() as conn, conn.cursor() as cursor:
                cursor.execute(f'CREATE DATABASE IF NOT EXISTS `{self.name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin')
        self.cover_cache=Database(path.with_name('cover-cache.sqlite3'))
        with self.connect() as connection:
            for statement in SCHEMA:
                connection.execute(statement+' DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin')
            connection.execute('CREATE INDEX IF NOT EXISTS catalog_status ON videos(status,rowid)')
        # Establish the first read connection before the API becomes ready.
        self.all('SELECT 1 AS ready')

    @contextmanager
    def connect(self):
        with self.lock:
            connection=self.idle.pop() if self.idle else tidb_connection(self.name)
            try:
                connection.ping(reconnect=True)
                yield TiDBConnection(connection)
                connection.commit()
            except BaseException:
                try:
                    connection.rollback()
                finally:
                    connection.close()
                raise
            else:
                self.idle.append(connection)

    def backup(self,target):
        copy_database(self,Database(target))

    def all(self,sql,args=()):
        # Independent SELECTs need neither a transaction commit nor the write
        # lock used to serialize queue claims. Bound connections per process.
        import pymysql
        with self.read_slots:
            for attempt in range(2):
                with self.read_lock:
                    connection=self.read_idle.pop() if self.read_idle else None
                try:
                    if connection is None:
                        connection=tidb_connection(self.name)
                        connection.autocommit(True)
                    with connection.cursor() as cursor:
                        cursor.execute(sql.replace('?','%s'),args or None)
                        rows=cursor.fetchall()
                except (pymysql.OperationalError,pymysql.InterfaceError):
                    if connection is not None and connection.open:
                        connection.close()
                    if attempt:
                        raise
                except BaseException:
                    if connection is not None and connection.open:
                        connection.close()
                    raise
                else:
                    with self.read_lock:
                        self.read_idle.append(connection)
                    return rows

    def videos_by_path(self, paths):
        paths = list(dict.fromkeys(paths))
        if not paths:
            return {}
        keys = ','.join('UNHEX(SHA2(?,256))' for _ in paths)
        rows = self.all(f'SELECT * FROM videos WHERE path_key IN ({keys})',paths)
        return {row['path']:row for row in rows}


def open_database(path):
    backend=os.getenv('AI_DB_BACKEND','sqlite').lower()
    if backend=='tidb':
        return TiDBDatabase(path)
    if backend!='sqlite':
        raise ValueError('AI_DB_BACKEND must be sqlite or tidb')
    return Database(path)


def copy_database(source, destination):
    """Copy a consistent snapshot into an empty database and verify every row."""
    from .protocols.store import SCHEMA as MEDIA_SCHEMA
    media_tables={
        'media_publications':'id data salt secret_hash revoked created updated',
        'media_index':'id vod_id path',
        'media_sessions':'secret_hash publication_id expires device_id',
        'media_user_state':'publication_id item_id position_ticks played favorite play_count last_played',
        'media_targets':'id item_id directory basename updated error',
        'media_export_jobs':'id publication_id state result created updated',
        'media_probes':'id fingerprint data updated',
        'media_preferences':'publication_id preference_key data',
    }
    # Older installations have no protocol tables. Do not instantiate MediaStore
    # here: backups must never rotate keys or reset running jobs.
    if source.backend=='sqlite':
        present={row['name'] for row in source.all("SELECT name FROM sqlite_master WHERE type='table'")}
    else:
        present={row['TABLE_NAME'] for row in source.all('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=?',(source.name,))}
    selected={name:fields for name,fields in media_tables.items() if name in present}
    for statement in MEDIA_SCHEMA:
        if any((' '+name+' ') in statement for name in selected):destination.execute(statement)
    counts={}
    with source.connect() as src, destination.connect() as dst:
        src.execute('BEGIN')
        dst.execute('BEGIN')
        for table,fields in {**TABLES,**selected}.items():
            names=fields.split()
            columns=','.join(f'`{name}`' for name in names)
            select=f'SELECT {columns} FROM `{table}` ORDER BY `{names[0]}`'
            if dst.execute(f'SELECT 1 FROM `{table}` LIMIT 1').fetchone():
                raise ValueError(f'Destination table {table} is not empty')
            values=[tuple(row[name] for name in names) for row in src.execute(select).fetchall()]
            if table=='covers':
                if destination.backend=='tidb':
                    cache=destination.cover_cache
                    with cache.connect() as local:
                        local.executemany('REPLACE INTO covers(path,fingerprint,sidecar,jpeg,updated) VALUES (?,?,?,?,?)',values)
                    values=[(path,fp,sidecar,None,updated) for path,fp,sidecar,_,updated in values]
                elif source.backend=='tidb':
                    local_rows={row['path']:row for row in source.cover_cache.all('SELECT * FROM covers')}
                    values=[(path,fp,sidecar,local_rows[path]['jpeg'] if path in local_rows and
                             local_rows[path]['fingerprint']==fp else jpeg,updated)
                            for path,fp,sidecar,jpeg,updated in values]
            placeholders=','.join('?' for _ in names)
            dst.executemany(f'INSERT INTO `{table}` ({columns}) VALUES ({placeholders})',values)
            actual=[tuple(row[name] for name in names) for row in dst.execute(select).fetchall()]
            from collections import Counter
            if Counter(values)!=Counter(actual):
                raise RuntimeError(f'Data verification failed for {table}')
            counts[table]=len(values)
    return counts
