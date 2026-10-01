"""Real CPU embedding + FAISS smoke, with outbound socket connections forbidden."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, required=True)
    args = parser.parse_args()

    def offline(event, _args):
        if event == 'socket.connect':
            raise RuntimeError('Network is forbidden in the embedding smoke')
    sys.addaudithook(offline)
    import numpy as np
    import faiss
    from rag.embeddings import load_local_embeddings
    from rag.index_schema import embedding_profile_id
    model = load_local_embeddings(args.cache)
    texts = ['Столица России — Москва.', 'Банк увеличил активы на десять процентов.',
             'Температура воды в море составляет двадцать градусов.']
    vectors = np.asarray(model.embed_documents(texts), dtype='float32')
    query = np.asarray([model.embed_query('Как называется столица России?')], dtype='float32')
    repeated = np.asarray([model.embed_query('Как называется столица России?')], dtype='float32')
    if vectors.shape != (3, 384) or not np.allclose(query, repeated, atol=1e-6):
        raise RuntimeError('Embedding dimensions/determinism check failed')
    index = faiss.IndexFlatL2(384)
    index.add(vectors)
    _, rows = index.search(query, 1)
    if int(rows[0, 0]) != 0:
        raise RuntimeError('Russian retrieval smoke failed')
    print(json.dumps({'status': 'passed', 'network': 'socket.connect forbidden',
                      'dimensions': 384, 'documents': 3, 'top_source': 0,
                      'profile': model.profile, 'embedding_profile_id': embedding_profile_id(model.profile)},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
