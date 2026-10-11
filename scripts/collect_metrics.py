"""Small model-free collector. No prompts, credentials or source text in its output."""
import argparse
import json
import math
import statistics
from collections import deque
from pathlib import Path
import signal
from threading import Event
import time
from rag.runtime_client import RuntimeClient
from rag.metrics import rotate_logs
from rag.paths import unlinked_path


def append_event(path,event):
    path=unlinked_path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    rotate_logs(path)
    with path.open('a',encoding='utf-8') as output: output.write(json.dumps(event,allow_nan=False)+'\n')


def summarize_stages(path):
    """At most 1000 recent records per stage; never return free-text fields."""
    rows={stage:deque(maxlen=1000) for stage in ('retrieval','generation')}
    try:
        with Path(path).open(encoding='utf-8') as source:
            consumed=0
            while consumed<11*1024*1024:
                line=source.readline(65537)
                if not line: break
                consumed+=len(line)
                if len(line)>65536: break
                try: event=json.loads(line)
                except ValueError: continue
                if isinstance(event,dict) and event.get('stage') in rows: rows[event['stage']].append(event)
    except OSError: return {}
    result={}
    for stage,events in rows.items():
        if not events: continue
        durations=sorted(e['duration_ms'] for e in events if e.get('status')=='ok'
                         and type(e.get('duration_ms')) in (float,int)
                         and math.isfinite(e['duration_ms']) and e['duration_ms']>=0)
        summary={'sample_count':len(events),'errors':sum(e.get('status')!='ok' for e in events),
                 'p50_ms':statistics.median(durations) if durations else None,
                 'p95_ms':durations[math.ceil(.95*len(durations))-1] if durations else None}
        for key in ('input_tokens','output_tokens','total_tokens'):
            values=[e[key] for e in events if type(e.get(key)) is int and e[key]>=0]
            summary[key+'_observed']=sum(values) if values else None
            summary[key+'_available_records']=len(values)
        result[stage]=summary
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args(); stop=Event()
    signal.signal(signal.SIGTERM,lambda *unused: stop.set())
    client=RuntimeClient.from_env('collector')
    while not stop.is_set():
        try:
            state=client.request('/health/ready')
            event={key:state.get(key) for key in ('ready','active','queued','queue_limit','resources')}
        except RuntimeError: event={'ready':False,'error':'runtime_unavailable'}
        append_event(args.output,{'timestamp':time.time(),**event,
                                  'stages':summarize_stages(args.output.parent/'metrics.jsonl')})
        if args.once or stop.wait(30): break


if __name__=='__main__': main()
