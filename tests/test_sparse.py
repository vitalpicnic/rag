import unittest
from rag.sparse import build_sparse, rank_sparse, fuse, tokenize


class SparseTests(unittest.TestCase):
    def setUp(self):
        self.index=build_sparse([{'chunk_id':'a','text':'Активы банка 42,15 млрд рублей'},
                                 {'chunk_id':'b','text':'Температура моря 20 градусов'}],'corpus-one')

    def test_numeric_unicode_tokens(self):
        self.assertIn('42.15',tokenize('АКТИВЫ ４２,１５ млрд'))
        self.assertEqual(rank_sparse(self.index,'активы 42.15','corpus-one')[0][0],'a')

    def test_stale_fingerprint_and_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError): rank_sparse(self.index,'банк','new-corpus')
        with self.assertRaises(ValueError): build_sparse([{'chunk_id':'a','text':'x'},{'chunk_id':'a','text':'y'}],'id')

    def test_rrf_ignores_raw_score_scales_and_deduplicates(self):
        one=fuse([('a',1000),('a',999),('b',-1)],[('b',0.1),('a',0)])
        two=fuse([('a',0),('b',1000)],[('b',900),('a',500)])
        self.assertEqual(one,two)
        self.assertEqual([item[0] for item in one],['a','b'])

    def test_empty_query_and_deterministic_ties(self):
        self.assertEqual(rank_sparse(self.index,'???','corpus-one'),[])
        index=build_sparse([{'chunk_id':'b','text':'банк'},{'chunk_id':'a','text':'банк'}],'id')
        self.assertEqual([item[0] for item in rank_sparse(index,'банк','id')],['a','b'])

    def test_experiment_rejects_other_bundle_and_unknown_dense_candidates(self):
        from scripts.sparse_index import experiment
        with self.assertRaises(ValueError): experiment(self.index,'банк',{'chunks_id':'other','results':[]})
        with self.assertRaises(ValueError): experiment(self.index,'банк',{'chunks_id':'corpus-one','results':[['unknown',0]]})
        report=experiment(self.index,'42.15',{'chunks_id':'corpus-one','results':[['b',100],['a',0]]})
        self.assertEqual(report['experiment'],'rrf')
        self.assertEqual(report['results'][0][0],'a')
