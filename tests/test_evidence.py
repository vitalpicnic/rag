"""Evidence handling without providers or network access."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.test_deps'))
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, AIMessage
from rag.evidence import source_metadata, resolve_query, select_evidence, format_context, finalize_answer, REFUSAL


class EvidenceTests(unittest.TestCase):
    def test_metadata_distinguishes_dates_and_ignores_unlabelled_header(self):
        meta = source_metadata('data/topic/ЦБ_Тренды_мар_2026.pdf', [
            'Декабрь 2021', 'По данным на 06.03.2026', 'Последнее обновление страницы: 07.03.2026'])
        self.assertEqual(meta['as_of'], '2026-03-06')
        self.assertEqual(meta['updated_at'], '2026-03-07')
        self.assertNotIn('publication_date', meta)
        self.assertEqual(meta['as_of_page'], 2)
        self.assertEqual(meta['publisher'], 'ЦБ')

    def test_followup_uses_user_context_never_assistant_claims(self):
        history = [HumanMessage(content='Активы Сбербанка на 1 декабря 2025 года?'),
                   AIMessage(content='Выдуманный банк 2035')]
        query = resolve_query('А на начало того же года?', history)
        self.assertIn('Сбербанка', query)
        self.assertIn('2025', query)
        self.assertNotIn('2035', query)
        self.assertEqual(resolve_query('Активы ВТБ в 2026?', history), 'Активы ВТБ в 2026?')

    def test_overview_diversity_and_context_budget(self):
        docs = [Document(page_content='x' * 1000, metadata={'source': source, 'page': i})
                for source in ['a.pdf', 'b.pdf', 'c.pdf'] for i in range(6)]
        selected = select_evidence(docs, 'overview')
        self.assertEqual(len(selected), 6)
        self.assertEqual(len({d.metadata['source'] for d in selected}), 3)
        self.assertLessEqual(len(format_context(selected)), 14000)
        self.assertEqual(len(select_evidence(docs, 'text')), 3)

    def test_citations_are_resolved_to_physical_pages(self):
        docs = [Document(page_content='42', metadata={'source': 'data/topic/a.pdf', 'page': 4})]
        context = format_context(docs)
        self.assertIn('[S1]', context)
        self.assertIn('страница: 5', context)
        answer = finalize_answer('Значение 42 [S1].', docs)
        self.assertIn('a.pdf', answer)
        self.assertIn('стр. 5', answer)
        self.assertEqual(finalize_answer('42 [S2]', docs), REFUSAL)
        self.assertEqual(finalize_answer('42', docs), REFUSAL)
        self.assertEqual(finalize_answer('INSUFFICIENT_EVIDENCE', docs), REFUSAL)

    def test_duplicate_chunks_and_oversized_context(self):
        doc = Document(page_content='a' * 20000, metadata={'source': 'a', 'page': 0})
        selected = select_evidence([doc, doc], 'overview')
        self.assertEqual(len(selected), 1)
        self.assertLessEqual(len(format_context(selected)), 14000)
        self.assertEqual(doc.page_content, 'a' * 20000)

    def test_dates_from_another_page_are_not_cited_as_current_page(self):
        doc = Document(page_content='text', metadata={'source': 'a.pdf', 'page': 0,
                       'as_of': '2026-03-06', 'as_of_page': 2})
        self.assertNotIn('2026-03-06', format_context([doc]))
        doc.metadata['page'] = 1
        self.assertIn('2026-03-06', format_context([doc]))

    def test_conflicting_or_invalid_dates_are_not_promoted(self):
        self.assertNotIn('as_of', source_metadata('a.pdf',
            ['По данным на 01.01.2026', 'По данным на 02.01.2026']))
        self.assertNotIn('as_of', source_metadata('a.pdf', ['По данным на 31.02.2026']))


if __name__ == '__main__':
    unittest.main()
