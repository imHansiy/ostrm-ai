"""Bounded ISO-BMFF header reads; never walk media packets or sample tables."""
import math
import re
import time
import httpx


class UnsupportedContainer(ValueError):
    pass


class ContainerChanged(ValueError):
    pass


class RangeReader:
    block_size=65536

    def __init__(self,url,timeout,cancelled,transport=None):
        self.url,self.deadline,self.cancelled=url,time.monotonic()+timeout,cancelled
        self.client=httpx.Client(transport=transport,trust_env=False,follow_redirects=False)
        self.blocks={};self.total=None;self.etag=None;self.bytes=0;self.atoms=0

    def read(self,offset,length):
        if offset<0 or length<0 or length>64:raise UnsupportedContainer('Invalid metadata field')
        result=bytearray()
        while length:
            if self.cancelled() or time.monotonic()>=self.deadline:raise UnsupportedContainer('Metadata deadline exceeded')
            block=offset//self.block_size*self.block_size
            if block not in self.blocks:
                if self.bytes>=4*self.block_size:raise UnsupportedContainer('Metadata range budget exceeded')
                end=block+self.block_size-1
                if self.total is not None:
                    if offset+length>self.total:raise UnsupportedContainer('Truncated metadata')
                    end=min(end,self.total-1)
                with self.client.stream('GET',self.url,headers={'Range':f'bytes={block}-{end}','Accept-Encoding':'identity'},
                                        timeout=max(.1,min(10,self.deadline-time.monotonic()))) as response:
                    response.raise_for_status()
                    match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
                    etag=response.headers.get('ETag','')
                    if response.status_code!=206 or not match or not etag or etag.startswith('W/'):
                        raise UnsupportedContainer('Source cannot validate bounded metadata ranges')
                    start,last,total=map(int,match.groups())
                    if start!=block or last>end or last<start or total<=last or response.headers.get('Content-Encoding','identity')!='identity':
                        raise ContainerChanged('Inconsistent metadata range')
                    if self.total is not None and (self.total!=total or self.etag!=etag):
                        raise ContainerChanged('Metadata entity changed')
                    self.total,self.etag=total,etag
                    data=bytearray()
                    for chunk in response.iter_bytes(32768):
                        if self.cancelled() or time.monotonic()>=self.deadline:raise UnsupportedContainer('Metadata deadline exceeded')
                        data.extend(chunk)
                        if len(data)>last-start+1:raise ContainerChanged('Metadata response exceeded its range')
                    if len(data)!=last-start+1:raise UnsupportedContainer('Incomplete metadata range')
                    self.bytes+=len(data);self.blocks[block]=bytes(data)
            data=self.blocks[block];position=offset-block;take=min(length,len(data)-position)
            if take<=0:raise UnsupportedContainer('Truncated metadata')
            result.extend(data[position:position+take]);offset+=take;length-=take
        return bytes(result)

    def boxes(self,start,end):
        position=start
        while position+8<=end:
            self.atoms+=1
            if self.atoms>256:raise UnsupportedContainer('Too many metadata atoms')
            header=self.read(position,8);size=int.from_bytes(header[:4],'big');kind=header[4:];width=8
            if size==1:size=int.from_bytes(self.read(position+8,8),'big');width=16
            elif size==0:size=end-position
            if size<width or position+size>end:raise UnsupportedContainer('Invalid atom bounds')
            yield kind,position+width,position+size
            position+=size


def clock(reader,start,end):
    version=reader.read(start,1)[0]
    if version not in (0,1):raise UnsupportedContainer('Unknown clock version')
    offset,width=(20,8) if version else (12,4)
    if start+offset+4+width>end:raise UnsupportedContainer('Truncated clock')
    scale=int.from_bytes(reader.read(start+offset,4),'big')
    duration=int.from_bytes(reader.read(start+offset+4,width),'big')
    if not scale or duration==(1<<(8*width))-1:return None
    return scale,duration


def track(reader,start,end):
    ticks=None;media_clock=None;video=False
    for kind,a,b in reader.boxes(start,end):
        if kind==b'tkhd':
            version=reader.read(a,1)[0]
            if version not in (0,1):raise UnsupportedContainer('Unknown track version')
            offset,width=(28,8) if version else (20,4)
            if a+offset+width>b:raise UnsupportedContainer('Truncated track clock')
            value=int.from_bytes(reader.read(a+offset,width),'big')
            if value!=(1<<(8*width))-1:ticks=value
        elif kind==b'mdia':
            for child,c,d in reader.boxes(a,b):
                if child==b'hdlr' and c+12<=d:video=reader.read(c+8,4)==b'vide'
                elif child==b'mdhd':media_clock=clock(reader,c,d)
    return (ticks,media_clock) if video else None


def value(movie_clock,video_track):
    ticks,media_clock=video_track
    if ticks and movie_clock:return ticks/movie_clock[0]
    if media_clock and media_clock[1]:return media_clock[1]/media_clock[0]
    if movie_clock and movie_clock[1]:return movie_clock[1]/movie_clock[0]
    return None


def mp4_duration(url,timeout,cancelled=lambda:False,*,transport=None):
    if not str(url).startswith(('http://','https://')):return None
    reader=RangeReader(url,timeout,cancelled,transport)
    try:
        first=reader.read(0,8)
        if first[4:] not in (b'ftyp',b'moov',b'mdat',b'free',b'wide',b'skip'):return None
        for kind,a,b in reader.boxes(0,reader.total):
            if kind!=b'moov':continue
            movie_clock=None;video_track=None
            for child,c,d in reader.boxes(a,b):
                if child==b'mvhd':movie_clock=clock(reader,c,d)
                elif child==b'trak' and video_track is None:video_track=track(reader,c,d)
                if movie_clock and video_track:
                    duration=value(movie_clock,video_track)
                    if duration and math.isfinite(duration) and duration>0:return duration
            if video_track:
                duration=value(movie_clock,video_track)
                if duration and math.isfinite(duration) and duration>0:return duration
            return None
    except UnsupportedContainer:
        return None
    finally:reader.client.close()
