"""Opt-in durable batches for historical failures, using the existing queue and controls."""
import argparse
import json
import time
from .openlist import clean_path

KEY = 'failure_recovery'


def terminal_error(message):
    return any(text in (message or '') for text in (
        '视频容器损坏', '找不到可识别的视频轨道', '文件不存在或已移动',
        'HTTP 401', 'HTTP 403', '视频超过大小上限', '源视频超过整段读取流量上限'))


class Recovery:
    def __init__(self, db):
        self.db = db
        self.next_check = 0

    def status(self):
        row = self.db.one('SELECT value FROM settings WHERE `key`=?', (KEY,))
        return json.loads(row['value']) if row else {'state': 'disabled'}

    def start(self, batch_size=20, directory_batch_size=3, rounds=2, interval=30, paused=False, exclude_roots=()):
        if not 1 <= batch_size <= 100 or not 0 <= directory_batch_size <= 20 or not 1 <= rounds <= 3 or interval < 0:
            raise ValueError('Invalid recovery limits')
        excluded = list(dict.fromkeys(clean_path(root).rstrip('/') or '/' for root in exclude_roots))
        def deferred(path):
            return any(root == '/' or path == root or path.startswith(root + '/') for root in excluded)
        with self.db.connect() as c:
            existing = c.execute('SELECT value FROM settings WHERE `key`=?', (KEY,)).fetchone()
            if existing and json.loads(existing['value'])['state'] in ('running', 'paused'):
                raise RuntimeError('已有恢复计划，请先恢复或停止原计划')
            # One task per currently failed video; never select obsolete failures of successful files.
            tasks = c.execute("""SELECT MIN(t.id) AS id,t.path,MIN(v.size) AS size FROM tasks t
                JOIN videos v ON v.path=t.path JOIN runs r ON r.id=t.run_id
                WHERE t.state='failed' AND v.status='failed' AND r.state IN ('done','failed')
                GROUP BY t.path ORDER BY CASE WHEN MIN(v.size)<1024 THEN 1 ELSE 0 END,size,id""").fetchall()
            directories = c.execute("""SELECT d.id,d.path FROM directories d JOIN runs r ON r.id=d.run_id
                WHERE d.state='failed' AND r.state IN ('done','failed') ORDER BY d.id""").fetchall()
            plan = {'state': 'paused' if paused else 'running', 'created': time.time(), 'round': 1, 'max_rounds': rounds,
                    'batch_size': batch_size, 'directory_batch_size': directory_batch_size,
                    'interval': interval, 'next_batch_at': 0, 'batches': 0,
                    'exclude_roots': excluded,
                    'deferred_tasks': sum(deferred(r['path']) for r in tasks),
                    'deferred_directories': sum(deferred(r['path']) for r in directories),
                    'task_ids': [r['id'] for r in tasks if not deferred(r['path'])],
                    'directory_ids': [r['id'] for r in directories if not deferred(r['path'])],
                    'task_cursor': 0, 'directory_cursor': 0, 'current_tasks': [], 'current_directories': []}
            c.execute('REPLACE INTO settings(`key`,value) VALUES (?,?)', (KEY, json.dumps(plan)))
        return self.summary(plan)

    def control(self, action):
        with self.db.connect() as c:
            row = c.execute('SELECT value FROM settings WHERE `key`=?', (KEY,)).fetchone()
            if not row:
                raise RuntimeError('没有恢复计划')
            plan = json.loads(row['value'])
            if action not in ('pause', 'resume', 'stop'):
                raise ValueError(action)
            plan['state'] = {'pause': 'paused', 'resume': 'running', 'stop': 'stopped'}[action]
            c.execute('UPDATE settings SET value=? WHERE `key`=?', (json.dumps(plan), KEY))
        return self.summary(plan)

    @staticmethod
    def summary(plan):
        result = {k: v for k, v in plan.items() if k not in ('task_ids', 'directory_ids')}
        result['total_tasks'] = len(plan.get('task_ids', []))
        result['total_directories'] = len(plan.get('directory_ids', []))
        return result

    def tick(self):
        if time.monotonic() < self.next_check:
            return False
        self.next_check = time.monotonic() + 10
        plan = self.status()
        if plan['state'] != 'running' or time.time() < plan['next_batch_at']:
            return False
        # Existing active work (including newly discovered files) drains before the next batch.
        if self.db.one("SELECT t.id FROM tasks t JOIN runs r ON r.id=t.run_id WHERE t.state IN ('queued','running') AND r.state='running' LIMIT 1"):
            return False
        directory_busy = bool(self.db.one("SELECT d.id FROM directories d JOIN runs r ON r.id=d.run_id WHERE d.state IN ('queued','running') AND r.state='running' LIMIT 1"))
        with self.db.connect() as c:
            # Re-read under the same write lock used by worker queue claims and UI controls.
            row = c.execute('SELECT value FROM settings WHERE `key`=?', (KEY,)).fetchone()
            plan = json.loads(row['value'])
            if plan['state'] != 'running':
                return False
            selected_tasks, selected_dirs, run_ids = [], [], set()
            for key, limit, selected in [('task', plan['batch_size'], selected_tasks),
                                          ('directory', 0 if directory_busy else plan['directory_batch_size'], selected_dirs)]:
                if limit <= 0:
                    continue
                ids, cursor = plan[key+'_ids'], plan[key+'_cursor']
                # At most 200 candidates per type per tick: a mostly-successful second
                # pass must not hold the TiDB queue write lock for thousands of reads.
                stop = min(len(ids), cursor + 200)
                candidates = ids[cursor:stop]
                slots = ','.join('?' for _ in candidates)
                if candidates and key == 'task':
                    rows = c.execute(f"""SELECT t.id,t.path,t.run_id,v.error AS video_error FROM tasks t
                        JOIN runs r ON r.id=t.run_id JOIN videos v ON v.path=t.path
                        WHERE t.id IN ({slots}) AND t.state='failed' AND v.status='failed'
                        AND r.state IN ('done','failed','running') AND NOT EXISTS (
                        SELECT 1 FROM tasks live WHERE live.path=t.path AND live.state IN ('queued','running'))""", candidates).fetchall()
                elif candidates:
                    rows = c.execute(f"""SELECT d.id,d.run_id FROM directories d JOIN runs r ON r.id=d.run_id
                        WHERE d.id IN ({slots}) AND d.state='failed' AND r.state IN ('done','failed','running')""", candidates).fetchall()
                else:
                    rows = []
                eligible = {row['id']: row for row in rows}
                while cursor < stop and len(selected) < limit:
                    identifier = ids[cursor]
                    cursor += 1
                    task = eligible.get(identifier)
                    if not task or (key == 'task' and plan['round'] > 1 and terminal_error(task['video_error'])):
                        continue
                    selected.append(identifier)
                    run_ids.add(task['run_id'])
                plan[key+'_cursor'] = cursor
            if selected_tasks:
                slots = ','.join('?' for _ in selected_tasks)
                c.execute(f"""UPDATE tasks SET state='queued',error='',fingerprint=(
                    SELECT fingerprint FROM videos WHERE videos.path=tasks.path) WHERE id IN ({slots})""", selected_tasks)
                c.execute(f"""UPDATE videos SET status='pending',error='' WHERE status='failed'
                    AND path IN (SELECT path FROM tasks WHERE id IN ({slots}))""", selected_tasks)
            if selected_dirs:
                slots = ','.join('?' for _ in selected_dirs)
                # Retain page/depth to continue the original directory cursor.
                c.execute(f"UPDATE directories SET state='queued' WHERE id IN ({slots})", selected_dirs)
            if run_ids:
                slots = ','.join('?' for _ in run_ids)
                c.execute(f"UPDATE runs SET state='running',error='' WHERE id IN ({slots})", list(run_ids))
            plan['current_tasks'] = selected_tasks
            if not directory_busy:
                plan['current_directories'] = selected_dirs
            if selected_tasks or selected_dirs:
                plan['batches'] += 1
                plan['next_batch_at'] = time.time() + plan['interval']
            elif not directory_busy:
                exhausted = plan['task_cursor'] >= len(plan['task_ids']) and (
                    not plan['directory_batch_size'] or plan['directory_cursor'] >= len(plan['directory_ids']))
                if exhausted and plan['round'] < plan['max_rounds']:
                    plan.update(round=plan['round']+1, task_cursor=0, directory_cursor=0,
                                next_batch_at=time.time()+max(300, plan['interval']))
                elif exhausted:
                    plan['state'] = 'done'
                    plan['finished'] = time.time()
            c.execute('UPDATE settings SET value=? WHERE `key`=?', (json.dumps(plan), KEY))
        return bool(selected_tasks or selected_dirs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'status', 'pause', 'resume', 'stop'])
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--directory-batch-size', type=int, default=3)
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--interval', type=int, default=30)
    parser.add_argument('--paused', action='store_true', help='Create the plan without feeding work yet')
    parser.add_argument('--exclude-root', action='append', default=[], help='Defer an unavailable mount without modifying its failures')
    args = parser.parse_args()
    from . import config
    from .storage import open_database
    recovery = Recovery(open_database(config.DATA / 'aitags.sqlite3'))
    if args.action == 'start':
        result = recovery.start(args.batch_size, args.directory_batch_size, args.rounds, args.interval, args.paused, args.exclude_root)
    elif args.action == 'status':
        result = recovery.summary(recovery.status())
    else:
        result = recovery.control(args.action)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
