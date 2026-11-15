"""Migrate a stopped plugin's SQLite database to a dedicated TiDB schema."""

import argparse
import json
from pathlib import Path

from . import config
from .db import Database
from .lock import WorkerLock
from .storage import TiDBDatabase, copy_database


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=config.DATA/'aitags.sqlite3')
    parser.add_argument('--init-only',action='store_true')
    args=parser.parse_args()
    config.DATA.mkdir(parents=True,exist_ok=True)
    if args.init_only:
        TiDBDatabase(config.DATA/'aitags.sqlite3',create=True)
        print('TiDB schema initialized.')
        return
    source_path=args.source.resolve()
    if not source_path.is_file():
        parser.error('SQLite source does not exist')
    lock=WorkerLock(str(source_path)+'.worker.lock')
    try:
        source=Database(source_path)
        for table in ('tasks','directories'):
            if source.one(f"SELECT id FROM {table} WHERE state='running' LIMIT 1"):
                parser.error(f'Drain active {table} and stop the plugin before migration')
        target=TiDBDatabase(config.DATA/'aitags.sqlite3',create=True)
        print(json.dumps(copy_database(source,target),sort_keys=True))
    finally:
        lock.close()


if __name__=='__main__':
    main()
