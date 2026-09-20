"""Real LangChain execution with local model/retriever substitutes; no API calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.test_deps'))
from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.documents import Document
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableLambda

from rag.pipeline import build_history_chain


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.sessions = {}
        self.prompts = []
        self.queries = []
        self.document = Document(page_content='The value is 42.',
                                 metadata={'source': 'data/topic/a.pdf', 'page': 0})
        self.prompt = ChatPromptTemplate.from_messages([
            ('system', '{context}'), MessagesPlaceholder('chat_history'), ('human', '{input}')])

    def history(self, session):
        return self.sessions.setdefault(session, InMemoryChatMessageHistory())

    def retrieve(self, query):
        self.queries.append(query)
        return [self.document]

    def generate(self, prompt):
        self.prompts.append(prompt.to_messages())
        return AIMessage(content='42 [S1]', usage_metadata={
            'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
            'output_token_details': {'reasoning': 5}})

    def test_usage_sources_and_history_survive_two_real_chain_invocations(self):
        chain = build_history_chain(RunnableLambda(self.retrieve), RunnableLambda(self.generate),
                                    self.prompt, self.history, model_name='gemini-2.5-flash-lite')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'metrics.jsonl'
            with patch.dict(os.environ, {'RAG_METRICS_PATH': str(path)}):
                result = chain.invoke({'input': 'first', 'request_id': 'one'},
                                      config={'configurable': {'session_id': 's'}})
                second = chain.invoke({'input': 'next'},
                                      config={'configurable': {'session_id': 's'}})
            events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(result['answer'].startswith('42 [S1]'))
        self.assertEqual(result['sources'], [self.document])
        self.assertEqual(result['request_id'], 'one')
        self.assertNotEqual(second['request_id'], 'one')
        self.assertEqual(self.queries, ['first', 'next'])
        self.assertIn('The value is 42.', self.prompts[1][0].content)
        self.assertIn('страница: 1', self.prompts[1][0].content)
        self.assertEqual([m.content for m in self.prompts[1][1:]],
                         ['first', result['answer'], 'next'])
        self.assertEqual(len(self.sessions['s'].messages), 4)
        generation = [e for e in events if e['stage'] == 'generation']
        self.assertEqual(len(generation), 2)
        self.assertTrue(all(e['model'] == 'gemini-2.5-flash-lite' for e in generation))
        self.assertEqual(generation[0]['output_tokens'], 20)
        self.assertEqual(generation[0]['thinking_tokens'], 5)

    def test_retrieval_failure_is_logged_without_invoking_model(self):
        def fail(query):
            raise RuntimeError('retrieval failure')
        chain = build_history_chain(RunnableLambda(fail), RunnableLambda(self.generate),
                                    self.prompt, self.history)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'metrics.jsonl'
            with patch.dict(os.environ, {'RAG_METRICS_PATH': str(path)}):
                with self.assertRaises(RuntimeError):
                    chain.invoke({'input': 'question'},
                                 config={'configurable': {'session_id': 's'}})
            event = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual((event['stage'], event['status']), ('retrieval', 'error'))
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.sessions['s'].messages, [])

    def test_oversized_input_never_calls_retriever_or_model(self):
        chain = build_history_chain(RunnableLambda(self.retrieve), RunnableLambda(self.generate),
                                    self.prompt, self.history)
        with self.assertRaises(ValueError):
            chain.invoke({'input': 'x' * 8001}, config={'configurable': {'session_id': 's'}})
        self.assertEqual(self.queries, [])
        self.assertEqual(self.prompts, [])

    def test_overview_selects_one_generation_and_prompt_history_is_bounded(self):
        from rag.history import estimated_tokens
        summary_prompts = []
        def summarize(prompt):
            summary_prompts.append(prompt.to_messages())
            return AIMessage(content='overview [S1]')
        self.history('s').add_messages([AIMessage(content='old answer')])
        from langchain_core.messages import HumanMessage
        for i in range(20):
            self.history('s').add_messages([HumanMessage(content='old question ' * 20), AIMessage(content='old answer ' * 20)])
        chain = build_history_chain(RunnableLambda(self.retrieve), RunnableLambda(self.generate),
            self.prompt, self.history, summary_llm=RunnableLambda(summarize), history_budget=80)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'RAG_METRICS_PATH': str(Path(tmp) / 'metrics.jsonl')}):
            result = chain.invoke({'input': 'overview', 'mode': 'overview'},
                                  config={'configurable': {'session_id': 's'}})
        self.assertTrue(result['answer'].startswith('overview [S1]'))
        self.assertEqual(len(summary_prompts), 1)
        self.assertEqual(self.prompts, [])
        self.assertLessEqual(estimated_tokens(summary_prompts[0][1:-1]), 80)

    def test_empty_evidence_refuses_without_generation(self):
        from rag.evidence import REFUSAL
        chain = build_history_chain(RunnableLambda(lambda query: []), RunnableLambda(self.generate),
                                    self.prompt, self.history)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'RAG_METRICS_PATH': str(Path(tmp) / 'metrics.jsonl')}):
            result = chain.invoke({'input': 'question'}, config={'configurable': {'session_id': 's'}})
        self.assertEqual(result['answer'], REFUSAL)
        self.assertEqual(self.prompts, [])

    def test_followup_query_and_overview_mode_reach_retriever(self):
        received = []
        def retrieve(query, config):
            received.append((query, config['metadata']['retrieval_mode']))
            return [self.document]
        self.history('s').add_user_message('Активы Сбербанка в 2025 году?')
        chain = build_history_chain(RunnableLambda(retrieve), RunnableLambda(self.generate),
                                    self.prompt, self.history)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'RAG_METRICS_PATH': str(Path(tmp) / 'metrics.jsonl')}):
            chain.invoke({'input': 'А на начало того же года?', 'mode': 'overview'},
                         config={'configurable': {'session_id': 's'}})
        self.assertIn('Сбербанка', received[0][0])
        self.assertEqual(received[0][1], 'overview')


if __name__ == '__main__':
    unittest.main()
