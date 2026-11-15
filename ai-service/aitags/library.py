"""Paged administrator catalog backed by the configured source database."""
import math
import posixpath
import re
import threading
import time
from collections import Counter
from datetime import datetime


CATEGORY_TAGS = {
    '电影': {'电影','影视','电影片段','剧情电影'},
    '动画': {'动画','动漫','动画电影','动画漫剪','日漫','国漫'},
    '游戏': {'游戏','游戏录像','游戏实况','游戏展示','游戏录屏'},
    '教程': {'教程','教学','编程','编程教程','操作演示','技术教程'},
    '旅行': {'旅行','旅游','旅拍','城市漫步'},
    '美食': {'美食','烹饪','料理','烘焙'},
    '音乐': {'音乐','演唱','演唱会','MV','舞蹈'},
    '纪录片': {'纪录片','纪实'},
    '自然风景': {'风景','自然','风光','花卉','荷花','山川','海洋'},
    '运动': {'运动','体育','健身','足球','篮球'},
    '屏幕录制': {'屏幕录制','录屏','软件演示'},
    '成人内容': {'成人内容','成人','NSFW','裸露','色情'},
}


def filename_year(path):
    name=posixpath.basename(path)
    for match in re.finditer(r'(?<!\d)((?:19|20)\d{2})(\d{4})?(?!\d)',name):
        if re.search(r'\d[xX×]$',name[:match.start()]) or re.match(r'[xX×]\d',name[match.end():]):
            continue
        if match[2]:
            try:
                datetime.strptime(match[0],'%Y%m%d')
            except ValueError:
                continue
        return match[1]
    return 'unknown'


def categories(item):
    tags={tag.casefold() for tag in item['tags']}
    labels=[label for label,values in CATEGORY_TAGS.items()
            if tags.intersection(value.casefold() for value in values)]
    if item['result'].get('sampling',{}).get('content_type')=='movie' and '电影' not in labels:
        labels.append('电影')
    return labels or ['其他']


class LibraryCatalog:
    def __init__(self,db):
        self.db=db
        self.facet_lock=threading.Lock()
        self.facet_cache={}

    def aliases(self):
        return {row['alias']:row['canonical'] for row in self.db.all('SELECT * FROM aliases')}

    def rows(self,prefix,sort,limit,offset=0,after=None,as_of=0):
        tidb=self.db.backend=='tidb'
        def value(field):
            expression=f"JSON_EXTRACT(v.result,'$.{field}')"
            return f'JSON_UNQUOTE({expression})' if tidb else expression
        join='c.path_key=v.path_key AND ' if tidb else ''
        name="LOWER(SUBSTRING_INDEX(v.path,'/',-1))" if tidb else 'LOWER(catalog_name(v.path))'
        order={'newest':'v.rowid DESC','oldest':'v.rowid ASC','name':name+' ASC,v.rowid DESC'}[sort]
        where="v.status='done' AND substr(v.path,1,?)=?"
        args=[len(prefix),prefix]
        if as_of:
            where+=' AND v.updated<=?'
            args.append(as_of)
        if after is not None:
            where+=' AND v.rowid'+('<' if sort=='newest' else '>')+'?'
            args.append(after)
        return self.db.all(f"""SELECT v.rowid AS row_cursor,v.path,v.fingerprint,v.status,
            v.size,v.modified,v.manual,v.removed,v.error,v.updated,
            JSON_OBJECT('tags',COALESCE(JSON_EXTRACT(v.result,'$.tags'),JSON_ARRAY()),
              'summary',COALESCE({value('summary')},''),
              'sampling',JSON_OBJECT('duration',COALESCE({value('sampling.duration')},0),
                'content_type',COALESCE({value('sampling.content_type')},''))) AS result,
            (c.jpeg IS NOT NULL OR c.sidecar IS NOT NULL) AS has_cover
            FROM videos v LEFT JOIN covers c ON {join}v.path=c.path AND v.fingerprint=c.fingerprint
            WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?""",(*args,limit,offset))

    def batches(self,prefix,sort,as_of=0):
        after=None
        offset=0
        while True:
            rows=self.rows(prefix,sort,1000,offset,after,as_of)
            yield rows
            if len(rows)<1000:
                return
            if sort=='name':
                offset+=len(rows)
            else:
                after=rows[-1]['row_cursor']

    def page(self,body,prefix,aliases):
        if body.status not in ('','done'):
            return [],0,1
        words=body.keywords.casefold().split()
        wanted={aliases.get(tag.strip(),tag.strip()).casefold() for tag in body.tags if tag.strip()}
        filtered=bool(words or wanted or body.category or body.year or body.content!='all')
        if not filtered:
            total=self.db.one("""SELECT COUNT(*) AS n FROM videos
                WHERE status='done' AND substr(path,1,?)=? AND updated<=?""",(len(prefix),prefix,body.as_of))['n']
            page=min(body.page,max(1,math.ceil(total/body.page_size)))
            return self.rows(prefix,body.sort,body.page_size,(page-1)*body.page_size,as_of=body.as_of),total,page
        selected=[]
        total=0
        start=(body.page-1)*body.page_size
        for batch in self.batches(prefix,body.sort,body.as_of):
            for row in batch:
                item=self.db.public_video(row,aliases)
                labels=categories(item)
                adult='成人内容' in labels
                text=' '.join([posixpath.basename(row['path']),*item['tags'],item['result'].get('summary','')]).casefold()
                matches=(all(word in text for word in words)
                         and wanted.issubset(tag.casefold() for tag in item['tags'])
                         and (not body.category or body.category in labels)
                         and (body.content=='all' or adult==(body.content=='adult'))
                         and (not body.year or filename_year(row['path'])==body.year))
                if not matches:
                    continue
                if start<=total<start+body.page_size:
                    selected.append(row)
                total+=1
        return selected,total,body.page

    def facets(self,prefix):
        # Only compact options are cached. File results and page totals stay live.
        now=time.monotonic()
        with self.facet_lock:
            cached=self.facet_cache.get(prefix)
            if cached and now-cached[0]<60:
                return cached[1]
            aliases=self.aliases()
            types,years=Counter(),Counter()
            for batch in self.batches(prefix,'newest'):
                for row in batch:
                    item=self.db.public_video(row,aliases)
                    types.update(categories(item))
                    years[filename_year(row['path'])]+=1
            result={'types':[{'value':label,'count':types[label]} for label in [*CATEGORY_TAGS,'其他'] if types[label]],
                    'years':[{'value':year,'count':years[year]} for year in sorted(years,reverse=True) if year!='unknown']}
            if years['unknown']:
                result['years'].append({'value':'unknown','count':years['unknown']})
            if len(self.facet_cache)>=16:
                oldest=min(self.facet_cache,key=lambda key:self.facet_cache[key][0])
                del self.facet_cache[oldest]
            self.facet_cache[prefix]=(now,result)
            return result
