import unittest
from types import SimpleNamespace

from rag.evaluation import score_retrieval, summarize_results, run_cases


class EvaluationTests(unittest.TestCase):
    def test_recall_deduplicates_chunks_and_uses_one_based_pdf_pages(self):
        gold = [{'source': 'data/Банкинг/a.pdf', 'page': 2},
                {'source': 'data/Банкинг/b.pdf', 'page': 3}]
        retrieved = [{'source': 'D:\\RAG\\data\\Банкинг\\a.pdf', 'page': 1}] * 3
        score = score_retrieval(gold, retrieved)
        self.assertEqual(score, {'page_recall': 0.5, 'all_pages_found': False})

    def test_same_filename_in_another_topic_does_not_match(self):
        score = score_retrieval([{'source': 'data/Банкинг/a.pdf', 'page': 1}],
                                [{'source': 'data/legal/a.pdf', 'page': 0}])
        self.assertEqual(score['page_recall'], 0)

    def test_out_of_corpus_is_not_automatically_scored_correct(self):
        self.assertEqual(score_retrieval([], []),
                         {'page_recall': None, 'all_pages_found': None})

    def test_unknown_reviews_and_failed_calls_not_counted_as_success(self):
        rows = [dict(status='ok', duration_ms=10, page_recall=1.0,
                     review={'answer_correct': True}),
                dict(status='error', duration_ms=30, page_recall=None, review={}),
                dict(status='ok', duration_ms=20, page_recall=0.0,
                     review={'answer_correct': None})]
        summary = summarize_results(rows)
        self.assertEqual(summary['error_count'], 1)
        self.assertEqual(summary['mean_page_recall'], 0.5)
        self.assertEqual(summary['answer_reviewed_count'], 1)
        self.assertEqual(summary['answer_accuracy_reviewed'], 1.0)
        self.assertEqual(summary['latency_p95_ms'], 30)

    def test_runner_isolates_history_and_keeps_errors_for_review(self):
        sessions, cleared = {}, []
        class Chain:
            def invoke(self, inputs, config):
                history = sessions[config['configurable']['session_id']]
                if inputs['input'] == 'bad':
                    self.last_history = history
                    raise RuntimeError('private error text')
                return {'answer': '42', 'sources': [SimpleNamespace(
                    metadata={'source': 'data/topic/a.pdf', 'page': 0}, page_content='42')]}
        cases = [dict(id='a', question='good', expected_answer='42', category='facts',
                      expected_sources=[{'source':'data/topic/a.pdf','page':1}],
                      history=[{'role':'user','content':'prior'}]),
                 dict(id='b', question='bad', expected_answer='none', category='facts',
                      expected_sources=[])]
        chain = Chain()
        def prepare(session, history):
            sessions[session] = history
        rows = run_cases(chain, cases, prepare, cleared.append)
        self.assertEqual(rows[0]['page_recall'], 1)
        self.assertEqual(rows[0]['answer'], '42')
        self.assertEqual(rows[1]['status'], 'error')
        self.assertEqual(rows[1]['error_type'], 'RuntimeError')
        self.assertNotIn('private error text', str(rows))
        self.assertEqual(chain.last_history, [])
        self.assertEqual(len(set(cleared)), 2)
        self.assertIsNone(rows[0]['review']['answer_correct'])

    def test_overview_mode_and_evidence_diagnostics_are_preserved(self):
        inputs_seen = []
        class Chain:
            def invoke(self, inputs, config):
                inputs_seen.append(inputs)
                return {'answer': 'answer', 'evidence_status': 'cited_unverified',
                        'retrieval_calibrated': False, 'sources': [SimpleNamespace(
                            metadata={'source': 'a', 'page': 0, 'retrieval_distance': 0.25},
                            page_content='text')]}
        rows = run_cases(Chain(), [dict(id='o', question='overview', expected_answer='answer',
                         category='overview', expected_sources=[])], lambda *args: None, lambda *args: None)
        self.assertEqual(inputs_seen[0]['mode'], 'overview')
        self.assertEqual(rows[0]['sources'][0]['retrieval_distance'], 0.25)
        self.assertEqual(rows[0]['evidence_status'], 'cited_unverified')
        self.assertFalse(rows[0]['retrieval_calibrated'])


if __name__ == '__main__':
    unittest.main()
