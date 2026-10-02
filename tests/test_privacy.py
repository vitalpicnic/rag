import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from rag.privacy import PolicyStore, PrivacyGate, PrivacyDenied


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'policy.json'
        self.policy = {'schema_version': 1, 'topics': {'bank': {
            'external_generation': 'allow',
            'providers': {'gemini': ['https://generativelanguage.googleapis.com']},
            'sources': {}}}}
        self.write()
        self.store = PolicyStore(self.path)
        self.gate = PrivacyGate(self.store, 'bank', 'gemini', 'https://generativelanguage.googleapis.com')

    def write(self): self.path.write_text(json.dumps(self.policy), encoding='utf-8')

    def test_missing_policy_and_topic_default_deny(self):
        self.path.unlink()
        with self.assertRaises(PrivacyDenied): self.gate.authorize(['source-a'])
        self.policy['topics'] = {}
        self.write()
        with self.assertRaises(PrivacyDenied): self.gate.authorize(['source-a'])

    def test_source_deny_overrides_topic_allow(self):
        self.policy['topics']['bank']['sources']['source-a'] = {'external_generation':'deny'}
        self.write()
        with self.assertRaises(PrivacyDenied): self.gate.authorize(['source-a'])

    def test_unapproved_endpoint_and_local_forbid_external(self):
        for gate in [PrivacyGate(self.store, 'bank','gemini','https://other.example'),
                     PrivacyGate(self.store, 'bank','gemini','https://generativelanguage.googleapis.com', profile='LOCAL')]:
            with self.assertRaises(PrivacyDenied): gate.authorize(['source-a'])

    def test_count_and_generation_are_blocked_after_revocation(self):
        lease = self.gate.authorize(['source-a'])
        self.policy['topics']['bank']['external_generation'] = 'deny'
        self.write()
        count, generate = Mock(), Mock()
        for call in (count, generate):
            with self.assertRaises(PrivacyDenied): self.gate.call(lease, call, 'private')
            call.assert_not_called()

    def test_inflight_result_is_discarded_on_revocation(self):
        lease = self.gate.authorize(['source-a'])
        def generate():
            self.policy['topics']['bank']['external_generation'] = 'deny'
            self.write()
            return 'private answer'
        with self.assertRaises(PrivacyDenied): self.gate.call(lease, generate)

    def test_malformed_policy_is_not_replaced_by_cached_allow(self):
        lease = self.gate.authorize(['source-a'])
        self.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(PrivacyDenied): self.gate.validate(lease)

    def test_endpoint_has_no_credentials_or_query(self):
        for endpoint in ['https://user:secret@example.com','https://@example.com','https://example.com?key=secret']:
            with self.assertRaises(PrivacyDenied): PrivacyGate(self.store,'bank','gemini',endpoint)

    def test_lease_cannot_be_used_by_another_gate(self):
        lease = self.gate.authorize(['source-a'])
        other = PrivacyGate(self.store,'bank','gemini','https://generativelanguage.googleapis.com')
        with self.assertRaises(PrivacyDenied): other.validate(lease)

    def test_missing_source_identity_is_denied(self):
        for sources in ([], [None], ['']):
            with self.assertRaises(PrivacyDenied): self.gate.authorize(sources)

    def test_endpoint_is_exact_and_malformed_values_fail_closed(self):
        for endpoint in [None, 42, 'http://example.com', 'https://example.com:bad',
                         'https://generativelanguage.googleapis.com/unapproved']:
            with self.assertRaises(PrivacyDenied):
                gate = PrivacyGate(self.store, 'bank', 'gemini', endpoint)
                gate.authorize(['source-a'])

    def test_any_policy_change_invalidates_existing_lease(self):
        lease = self.gate.authorize(['source-a'])
        self.policy['topics']['other'] = {'external_generation': 'deny'}
        self.write()
        with self.assertRaises(PrivacyDenied): self.gate.validate(lease)
        self.gate.authorize(['source-a'])

    def chain(self, gate=True):
        from langchain_core.chat_history import InMemoryChatMessageHistory
        from langchain_core.documents import Document
        from langchain_core.messages import AIMessage
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
        from langchain_core.runnables import RunnableLambda
        from rag.pipeline import build_history_chain
        self.calls = []
        def generate(prompt):
            self.calls.append(prompt)
            if getattr(self, 'revoke_in_generation', False):
                self.policy['topics']['bank']['external_generation'] = 'deny'
                self.write()
            return AIMessage(content='Assets are 42 [S1].')
        retriever = RunnableLambda(lambda _: [Document(page_content='Assets are 42.',
                                  metadata={'source_id':'source-a','source':'data/bank/a.txt','page':0})])
        prompt = ChatPromptTemplate.from_messages([('system','{context}'), MessagesPlaceholder('chat_history'), ('human','{input}')])
        history = InMemoryChatMessageHistory()
        return build_history_chain(retriever, RunnableLambda(generate), prompt, lambda _: history,
                                   privacy_gate=self.gate if gate else None)

    def test_pipeline_denies_before_generation(self):
        chain = self.chain()
        self.policy['topics']['bank']['external_generation'] = 'deny'
        self.write()
        with self.assertRaises(PrivacyDenied):
            chain.invoke({'input':'assets'}, config={'configurable':{'session_id':'one'}})
        self.assertEqual(self.calls, [])

    def test_pipeline_refuses_untracked_external_history(self):
        chain = self.chain()
        config = {'configurable':{'session_id':'one'}}
        result = chain.invoke({'input':'assets'}, config=config)
        self.assertIn('42', result['answer'])
        with self.assertRaises(PrivacyDenied): chain.invoke({'input':'again'}, config=config)
        self.assertEqual(len(self.calls), 1)

    def test_production_pipeline_requires_explicit_gate(self):
        with patch.dict('os.environ', {'RAG_ENV':'production'}):
            with self.assertRaises(PrivacyDenied): self.chain(gate=False)

    def test_pipeline_discards_revoked_result(self):
        self.revoke_in_generation = True
        chain = self.chain()
        with self.assertRaises(PrivacyDenied):
            chain.invoke({'input':'assets'}, config={'configurable':{'session_id':'one'}})
        self.assertEqual(len(self.calls), 1)
