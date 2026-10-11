import argparse
from rag.operations import backup

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Stop runtime and all writers before backup')
    parser.add_argument('root'); parser.add_argument('archive')
    args=parser.parse_args(); backup(args.root,args.archive)
    print('Backup verified and written')
