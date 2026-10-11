import argparse
from rag.runtime_client import RuntimeClient
from rag.health import heartbeat_ready


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--heartbeat')
    parser.add_argument('--ready',action='store_true')
    args=parser.parse_args()
    if args.heartbeat: ok=heartbeat_ready(args.heartbeat)
    else:
        result=RuntimeClient.from_env('health').request('/health/ready' if args.ready else '/health/live')
        ok=result.get('ready' if args.ready else 'live',False)
    raise SystemExit(0 if ok else 1)


if __name__ == '__main__': main()
