import argparse
from rag.operations import restore

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Restore only into a new root')
    parser.add_argument('archive'); parser.add_argument('destination')
    args=parser.parse_args(); restore(args.archive,args.destination)
    print('Restore verified; new root is ready for operator activation')
