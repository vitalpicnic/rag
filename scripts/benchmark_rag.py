"""Manual load measurements. Output deliberately excludes questions and answers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import statistics
import time
from uuid import uuid4
from rag.runtime_client import RuntimeClient


def measure(call,count=30,clients=1):
    if count<30 or clients not in (1,2,4): raise ValueError('Use >=30 requests and 1/2/4 clients')
    def one(number):
        start=time.monotonic()
        try:
            result=call(number)
            return time.monotonic()-start,True,result.get('index_version')
        except Exception: return time.monotonic()-start,False,None
    with ThreadPoolExecutor(clients) as pool: rows=list(pool.map(one,range(count)))
    durations=sorted(row[0] for row in rows if row[1])
    return {'clients':clients,'attempted':count,'completed':len(durations),'errors':count-len(durations),
            'p50_seconds':statistics.median(durations) if durations else None,
            'p95_seconds':durations[math.ceil(.95*len(durations))-1] if durations else None,
            'index_versions':sorted(set(row[2] for row in rows if row[2])),
            'quality_evaluated':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--topic',required=True); parser.add_argument('--question',required=True)
    parser.add_argument('--mode',choices=['text','overview','executive','expert'],default='text')
    args=parser.parse_args()
    with args.output.open('x',encoding='utf-8') as output:
        client=RuntimeClient.from_env('benchmark')
        reports=[]
        for concurrency in (1,2,4):
            reports.append(measure(lambda n:client.query({'topic':args.topic,'question':args.question,
                    'session':uuid4().hex,'mode':args.mode}),clients=concurrency))
        json.dump({'schema_version':1,'mode':args.mode,'runs':reports},output,indent=2)


if __name__=='__main__': main()
