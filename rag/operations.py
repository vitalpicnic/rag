"""Maintenance-only backups and verified restore into a new, inactive root."""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tarfile
import tempfile
import time

from rag.resources import ModelOwnerLock

DIRECTORIES = ('data','faiss_indexes','prepared_tables','policies')


def resource_snapshot(root):
    """Own-process/container counters. Missing platform metrics remain unavailable."""
    result={'timestamp':time.time(),'rss_bytes':None,'rss_peak_bytes':None,
            'cgroup_memory_bytes':None,'cgroup_peak_bytes':None,'cpu_steal_ticks':None,'cpu_stat':None}
    try:
        fields=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
        result['rss_bytes']=int(fields['VmRSS'].split()[0])*1024
        result['rss_peak_bytes']=int(fields['VmHWM'].split()[0])*1024
    except (OSError,KeyError,ValueError): pass
    for filename,key in [('memory.current','cgroup_memory_bytes'),('memory.peak','cgroup_peak_bytes')]:
        try: result[key]=int((Path('/sys/fs/cgroup')/filename).read_text())
        except (OSError,ValueError): pass
    try:
        result['cpu_stat']={key:int(value) for key,value in (line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())}
    except (OSError,ValueError): pass
    try: result['cpu_steal_ticks']=int(Path('/proc/stat').read_text().splitlines()[0].split()[8])
    except (OSError,ValueError,IndexError): pass
    disk=shutil.disk_usage(root)
    result.update(disk_total_bytes=disk.total,disk_free_bytes=disk.free,disk_reserve_ok=disk.free>=disk.total*.2)
    try:
        total=0
        for number,path in enumerate((Path(root)/'faiss_indexes').rglob('*')):
            if number>=100000: raise ValueError('Index metric scan limit')
            if path.resolve()!=path.absolute(): raise ValueError('Linked index metric path')
            if path.is_file(): total+=path.stat().st_size
        result['index_bytes']=total
    except (OSError,ValueError): result['index_bytes']=None
    return result


def _safe(name):
    parts = PurePosixPath(name).parts
    if (not parts or name.startswith('/') or '\\' in name or ':' in name
            or any(part in ('..','.') for part in parts)
            or name != PurePosixPath(name).as_posix()
            or parts[0] not in DIRECTORIES):
        raise ValueError('Unsafe backup member')
    return parts


def _hash(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()


def verify_restored(root):
    root=Path(root)
    for path in (root/'prepared_tables').glob('*.sqlite'):
        connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            if connection.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise ValueError('SQLite integrity check failed')
        finally: connection.close()
    for pointer in (root/'faiss_indexes').glob('*/v2/active.json'):
        from rag.index_store import current_bundle,load_bundle
        import numpy as np
        topic=pointer.parent.parent.name
        index,rows=load_bundle(root,topic,current_bundle(root,topic))
        vector=np.asarray([index.reconstruct(0)],dtype='float32')
        distance,_=index.search(vector,1)
        if not rows or float(distance[0][0]) > 1e-5: raise ValueError('Restored retrieval smoke failed')


def backup(root, archive):
    root,archive=Path(root).resolve(),Path(archive).absolute()
    if archive.exists(): raise FileExistsError(archive)
    if any(archive.is_relative_to(root/name) for name in DIRECTORIES):
        raise ValueError('Backup destination must be outside the archived directories')
    with ModelOwnerLock(root):
        verify_restored(root)
        files=[]
        for directory in DIRECTORIES:
            base=root/directory
            if base.is_symlink(): raise ValueError('Linked backup directory forbidden')
            for path in sorted(base.rglob('*')) if base.exists() else []:
                if path.resolve()!=path: raise ValueError('Linked backup member forbidden')
                if not path.is_file(): continue
                if any(part.startswith('.env') or part=='secrets' for part in path.relative_to(root).parts):
                    raise ValueError('Secret-like file inside backup corpus; move it before backup')
                name=path.relative_to(root).as_posix(); _safe(name)
                files.append((path,name,path.stat().st_size,_hash(path)))
        manifest={'schema_version':1,'files':{name:{'size':size,'sha256':digest} for _,name,size,digest in files}}
        archive.parent.mkdir(parents=True,exist_ok=True)
        # Incomplete archives remain visibly incomplete; existing backups are never replaced.
        with archive.open('xb') as raw:
            os.chmod(archive,0o600)
            with tarfile.open(fileobj=raw,mode='w:gz') as output:
                for path,name,size,digest in files:
                    info=tarfile.TarInfo(name); info.size=size; info.mode=0o600
                    with path.open('rb') as source: output.addfile(info,source)
                    if path.stat().st_size!=size or _hash(path)!=digest:
                        raise ValueError('Source changed during backup')
                data=json.dumps(manifest,sort_keys=True).encode()
                info=tarfile.TarInfo('manifest.json'); info.size=len(data); info.mode=0o600
                output.addfile(info,io.BytesIO(data))
            raw.flush(); os.fsync(raw.fileno())
    return archive


def restore(archive, destination):
    destination=Path(destination).absolute()
    if destination.exists(): raise FileExistsError(destination)
    if destination.parent.resolve()!=destination.parent: raise ValueError('Linked restore parent forbidden')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(archive,'r:gz') as source:
        members=source.getmembers()
        names=set()
        if len(members)>100001: raise ValueError('Too many archive members')
        for member in members:
            if not member.isfile() or member.name in names or member.size<0:
                raise ValueError('Invalid or duplicate archive member')
            names.add(member.name)
            if member.name!='manifest.json': _safe(member.name)
        if 'manifest.json' not in names: raise ValueError('Missing backup manifest')
        info=source.getmember('manifest.json')
        if info.size>16*1024*1024: raise ValueError('Oversized backup manifest')
        manifest=json.loads(source.extractfile(info).read())
        records=manifest.get('files')
        if manifest.get('schema_version')!=1 or not isinstance(records,dict) or set(records)!=names-{'manifest.json'}:
            raise ValueError('Backup manifest does not match archive')
        total=sum(member.size for member in members)
        if total>shutil.disk_usage(destination.parent).free*0.8: raise ValueError('Insufficient restore disk reserve')
        with tempfile.TemporaryDirectory(prefix=destination.name+'.restore-',dir=destination.parent) as temporary:
            staging=Path(temporary)/'root'; staging.mkdir(mode=0o700)
            for member in members:
                if member.name=='manifest.json': continue
                record=records[member.name]
                if record.get('size')!=member.size: raise ValueError('Backup size mismatch')
                target=staging.joinpath(*_safe(member.name)); target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                with source.extractfile(member) as incoming, target.open('xb') as outgoing:
                    shutil.copyfileobj(incoming,outgoing,1024*1024)
                    outgoing.flush(); os.fsync(outgoing.fileno())
                os.chmod(target,0o600)
                if _hash(target)!=record.get('sha256'): raise ValueError('Backup checksum mismatch')
            verify_restored(staging)
            if destination.exists(): raise FileExistsError(destination)
            os.rename(staging,destination)
    return destination
