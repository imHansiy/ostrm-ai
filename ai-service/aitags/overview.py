"""Read-only operational summary, keeping catalog results separate from live queues."""
import time


def snapshot(db, worker):
    plan = worker.recovery.summary(worker.recovery.status())
    roots = list(dict.fromkeys(plan.get('exclude_roots', [])))
    terms, args = [], []
    for root in roots:
        root = root.rstrip('/') or '/'
        if root == '/':
            terms.append('1=1')
        else:
            terms.append('(path=? OR SUBSTR(path,1,?)=?)')
            args.extend((root, len(root)+1, root+'/'))
    scope = "CASE WHEN " + (' OR '.join(terms) or '1=0') + " THEN 'deferred' ELSE 'included' END"
    scopes = {name: {'videos': {}, 'tasks': {}, 'directories': {}} for name in ('included', 'deferred')}
    # Use read connections; dashboard polling must not take the queue's write lock.
    for row in db.all(f'SELECT {scope} AS scope,status,COUNT(*) AS n FROM videos GROUP BY scope,status', args):
        scopes[row['scope']]['videos'][row['status']] = row['n']
    for row in db.all(f"""SELECT {scope} AS scope,t.state,COUNT(*) AS n FROM tasks t
        JOIN runs r ON r.id=t.run_id WHERE (t.state='queued' AND r.state='running') OR t.state='running'
        GROUP BY scope,t.state""", args):
        scopes[row['scope']]['tasks'][row['state']] = row['n']
    # Qualify path for joins; prefix matching is literal (not SQL LIKE wildcards).
    dir_scope = scope.replace('path', 'd.path')
    for row in db.all(f"""SELECT {dir_scope} AS scope,d.state,r.state AS run_state,COUNT(*) AS n
        FROM directories d JOIN runs r ON r.id=d.run_id
        WHERE r.state IN ('running','paused') GROUP BY scope,d.state,r.state""", args):
        state = 'paused' if row['run_state']=='paused' and row['state']=='queued' else row['state']
        counts = scopes[row['scope']]['directories']
        counts[state] = counts.get(state, 0) + row['n']
    def combined(field):
        result = {}
        for counts in scopes.values():
            for key, value in counts[field].items():
                result[key] = result.get(key, 0) + value
        return result
    return {
        'videos': combined('videos'), 'tasks': combined('tasks'),
        'scans': {r['state']:r['n'] for r in db.all("SELECT state,COUNT(*) AS n FROM runs WHERE state IN ('running','paused') GROUP BY state")},
        'scopes': scopes, 'directories': combined('directories'), 'recovery':plan,
        'background':db.one('SELECT COUNT(*) AS pending,SUM(CASE WHEN attempts>0 THEN 1 ELSE 0 END) AS retrying FROM postprocess_jobs'),
        'activity':worker.activity.snapshot(),
        'last_success':db.one("SELECT path,created FROM history WHERE state='done' ORDER BY id DESC LIMIT 1"),
        'generated_at':time.time(),
    }
