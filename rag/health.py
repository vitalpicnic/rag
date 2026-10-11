"""Process/event-loop heartbeat; reads never trigger model or paid API calls."""
import asyncio
import json
import os
from pathlib import Path
import time


def heartbeat(path):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'pid':os.getpid(),'timestamp':time.time()}),encoding='utf-8')
    os.replace(temporary,path)


def heartbeat_ready(path, max_age=15):
    try:
        value = json.loads(Path(path).read_text())['timestamp']
        return type(value) in (int,float) and 0 <= time.time()-value <= max_age
    except (OSError,ValueError,KeyError,TypeError): return False


async def event_loop_heartbeat(path):
    while True:
        heartbeat(path)
        await asyncio.sleep(1)
