"""Evaluate lexical candidates against a verified active v2 bundle; no LLM required."""
import argparse
import json
from pathlib import Path
from rag.index_store import current_bundle,load_bundle
from rag.resources import ModelOwnerLock
from rag.sparse import build_sparse,rank_sparse,fuse


def experiment(index,question,dense=None):
    sparse=rank_sparse(index,question,index['chunks_id'])
    results=sparse
    if dense is not None:
        known={doc['id'] for doc in index['documents']}
        if (not isinstance(dense,dict) or dense.get('chunks_id')!=index['chunks_id']
                or not isinstance(dense.get('results'),list) or len(dense['results'])>100
                or any(not isinstance(row,list) or len(row)!=2 or not isinstance(row[0],str)
                       or row[0] not in known for row in dense['results'])):
            raise ValueError('Dense candidates must belong to this verified bundle')
        results=fuse(dense['results'],sparse)
    return {'experiment':'rrf' if dense is not None else 'sparse-only',
            'chunks_id':index['chunks_id'],'results':results}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--topic',required=True)
    parser.add_argument('--question',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--save-index',type=Path,help='New private JSON sparse store')
    parser.add_argument('--dense-results',type=Path,help='JSON with chunks_id and ranked results [[chunk_id,score],...]')
    args=parser.parse_args()
    if args.output.exists(): raise FileExistsError('Preserve existing experiment results')
    if args.save_index and (args.save_index.exists() or args.save_index.absolute()==args.output.absolute()):
        raise FileExistsError('Sparse store must have a distinct new path')
    if args.dense_results and args.dense_results.stat().st_size>65536: raise ValueError('Dense ranking too large')
    dense=json.loads(args.dense_results.read_text(encoding='utf-8')) if args.dense_results else None
    with ModelOwnerLock(args.root):
        bundle=current_bundle(args.root,args.topic)
        _,rows=load_bundle(args.root,args.topic,bundle)
        index=build_sparse(rows,bundle['chunks_id'])
        report=experiment(index,args.question,dense)
        if args.save_index:
            with args.save_index.open('x',encoding='utf-8') as handle: json.dump(index,handle,ensure_ascii=False)
        with args.output.open('x',encoding='utf-8') as handle:
            json.dump(report,handle)


if __name__=='__main__': main()
