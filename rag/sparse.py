"""Optional lexical retrieval experiment. Does not change dense indexes or defaults."""
from collections import Counter
import math
import re
import unicodedata

TOKENIZER='nfkc-casefold-numbers-v1'


def tokenize(text):
    normalized=unicodedata.normalize('NFKC',text).casefold()
    words=re.findall(r'\d+(?:[.,]\d+)*%?|[^\W\d_]+',normalized)
    return [word.replace(',','.') if word[0].isdigit() else word for word in words]


def build_sparse(chunks,chunks_id):
    if not isinstance(chunks_id,str) or not chunks_id: raise ValueError('chunks_id required')
    documents=[]; seen=set()
    for chunk in chunks:
        identity=chunk['chunk_id']
        if not isinstance(identity,str) or not identity or identity in seen: raise ValueError('Invalid/duplicate chunk ID')
        seen.add(identity)
        documents.append({'id':identity,'terms':dict(Counter(tokenize(chunk['text'])))})
    if len(documents)>100000: raise ValueError('Sparse chunk limit exceeded')
    return {'schema_version':1,'tokenizer':TOKENIZER,'chunks_id':chunks_id,'documents':documents}


def rank_sparse(index,question,chunks_id,limit=20):
    if (index.get('schema_version')!=1 or index.get('tokenizer')!=TOKENIZER
            or index.get('chunks_id')!=chunks_id): raise ValueError('Stale/unknown sparse index')
    if type(limit) is not int or not 1<=limit<=100: raise ValueError('Invalid result limit')
    documents=index['documents']; query=set(tokenize(question))
    if not documents or not query: return []
    lengths=[sum(doc['terms'].values()) for doc in documents]
    average=sum(lengths)/len(documents) or 1
    frequency=Counter(term for doc in documents for term in doc['terms'])
    scores=[]
    for doc,length in zip(documents,lengths):
        score=0.0
        for term in query:
            tf=doc['terms'].get(term,0)
            if tf:
                idf=math.log(1+(len(documents)-frequency[term]+0.5)/(frequency[term]+0.5))
                score+=idf*tf*2.2/(tf+1.2*(0.25+0.75*length/average))
        if score: scores.append((doc['id'],score))
    return sorted(scores,key=lambda item:(-item[1],item[0]))[:limit]


def fuse(dense,sparse,k=60):
    if type(k) is not int or k<=0: raise ValueError('Invalid RRF constant')
    scores=Counter()
    for ranking in (dense,sparse):
        for rank,identity in enumerate(dict.fromkeys(item[0] for item in ranking),1):
            scores[identity]+=1/(k+rank)
    return sorted(scores.items(),key=lambda item:(-item[1],item[0]))
