import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.test_deps'))
from langchain_core.documents import Document
from rag.analysis import response_instructions, retrieval_size
from rag.evidence import select_evidence, format_context


class AnalysisTests(unittest.TestCase):
    def test_roles_change_search_depth_and_instructions(self):
        self.assertGreater(retrieval_size('executive'), retrieval_size('text'))
        self.assertGreater(retrieval_size('expert'), retrieval_size('text'))
        self.assertIn('гипотез', response_instructions('executive'))
        self.assertIn('методик', response_instructions('expert'))
        with self.assertRaises(ValueError):
            response_instructions('invalid')

    def test_official_first_without_losing_alternative_and_expert_depth(self):
        docs = [Document(page_content=f'Фрагмент {i}', metadata={'source': 'review.pdf', 'page': i})
                for i in range(8)]
        docs.append(Document(page_content='Отчёт 42', metadata={
            'source': 'report.pdf', 'page': 0, 'source_kind': 'official_report'}))
        selected = select_evidence(docs, 'expert')
        self.assertEqual(selected[0].metadata['source'], 'report.pdf')
        self.assertIn('review.pdf', [d.metadata['source'] for d in selected])
        self.assertGreater(len(selected), 3)
        self.assertIn('official_report', format_context(selected))

    def test_executive_diversity_and_alternative_survive_many_official_chunks(self):
        docs = [Document(page_content=f'Факт {i}', metadata={
            'source': f'report{i // 3}.pdf', 'page': i, 'source_kind': 'official_report'})
                for i in range(12)]
        docs.append(Document(page_content='Другая оценка', metadata={'source': 'alternative.pdf'}))
        selected = select_evidence(docs, 'executive')
        self.assertIn('alternative.pdf', [d.metadata['source'] for d in selected])
        self.assertLessEqual(len(format_context(selected)), 24000)


if __name__ == '__main__':
    unittest.main()
