"""Deterministic model contracts exercise packing without a running LLM."""
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, AIMessage
from rag.token_budget import TokenContract, TokenCounter, BudgetDenied, pack_request, audit_usage


def exact_test_counter(messages):
    # A deliberately different, deterministic test tokenizer, not a production estimate.
    return 3 + sum(len(m.content.encode('utf-8')) + 4 for m in messages)


def doc(text, kind='analysis', source='a'):
    return Document(page_content=text, metadata={'source':source, 'source_id':source,
                                                'source_kind':kind, 'page':0})


class TokenBudgetTests(unittest.TestCase):
    def setUp(self):
        self.contract = TokenContract('test-model', 'test-tokenizer-v1', 16384, 4096,
                                      context_limit=20000, thinking_tokens=0)
        self.counter = TokenCounter(self.contract, exact_test_counter)

    def pack(self, **kwargs):
        return pack_request('System', 'Вопрос 🏦?', [], [doc('Активы | 42,15 млрд руб. | 2025')],
                            'text', kwargs.pop('provider', self.counter), **kwargs)

    def test_all_roles_unicode_and_final_payload_exact(self):
        for mode, output in [('text',600),('overview',1800),('executive',1800),('expert',3000)]:
            packed = pack_request('Правила 🏦', 'Каковы активы?', [],
                                  [doc('Активы | 42,15 млрд руб. | 2025')], mode, self.counter)
            self.assertEqual(packed.input_tokens, exact_test_counter(packed.messages))
            self.assertEqual(packed.output_tokens, output)
            self.assertLessEqual(packed.input_tokens + packed.margin, packed.effective_limit)
            self.assertIn('42,15 млрд руб.', packed.messages[0].content)

    def test_output_thinking_and_shared_window_reserved(self):
        counter = TokenCounter(replace(self.contract, context_limit=5000, thinking_tokens=100), exact_test_counter)
        packed = self.pack(provider=counter)
        self.assertEqual(packed.effective_limit, 4300)
        self.assertLessEqual(packed.input_tokens + packed.margin + packed.output_tokens + 100, 5000)

    def test_history_removes_oldest_whole_pairs(self):
        history = [HumanMessage(content='old'*200), AIMessage(content='answer'*100),
                   HumanMessage(content='new'), AIMessage(content='recent')]
        packed = pack_request('System','Q',history,[doc('42')],'text',self.counter)
        self.assertEqual([m.content for m in packed.history], ['new','recent'])
        self.assertEqual([m.content for m in history[:1]], ['old'*200])

    def test_evidence_is_never_sliced_and_official_then_alternative(self):
        docs = [doc('alternative', source='alt'), doc('official','official_report','official'),
                doc('huge'*10000, source='large')]
        packed = pack_request('System','Q',[],docs,'text',self.counter)
        self.assertEqual([d.page_content for d in packed.evidence], ['official','alternative'])
        self.assertIn('[S2]', packed.messages[0].content)
        self.assertEqual(docs[-1].page_content, 'huge'*10000)

    def test_oversized_mandatory_payload_and_no_evidence_fail(self):
        for system, question, docs in [('x'*20000,'Q',[doc('42')]),('S','Q'*20000,[doc('42')]),
                                       ('S','Q',[]),('S','Q',[doc('x'*20000)])]:
            with self.assertRaises(BudgetDenied):
                pack_request(system, question, [], docs, 'text', self.counter)

    def test_count_errors_and_invalid_results_fail_closed(self):
        for value in [None, True, -1, 1.5]:
            with self.assertRaises(BudgetDenied): self.pack(provider=TokenCounter(self.contract, lambda _: value))
        with self.assertRaises(BudgetDenied):
            self.pack(provider=TokenCounter(self.contract, Mock(side_effect=RuntimeError('secret'))))

    def test_unknown_contract_rejected(self):
        for changes in [{'counter_id':''}, {'input_limit':0}, {'thinking_tokens':None}]:
            with self.assertRaises(BudgetDenied): self.pack(provider=TokenCounter(replace(self.contract, **changes), exact_test_counter))

    def test_external_count_requires_gate_before_any_call(self):
        call = Mock(return_value=1)
        with self.assertRaises(BudgetDenied):
            self.pack(provider=TokenCounter(replace(self.contract, external=True), call))
        call.assert_not_called()

    def test_actual_usage_audit_has_no_text(self):
        packed = self.pack()
        with patch('rag.token_budget.write_event') as write:
            audit_usage(packed, {'input_tokens':packed.input_tokens+9}, 'test-model', 'request')
        event = write.call_args.args[0]
        self.assertEqual(event['input_token_delta'], 9)
        self.assertNotIn('Вопрос', str(event))

    def test_alternative_tokenizer_changes_packing(self):
        counter = TokenCounter(self.contract, lambda ms: exact_test_counter(ms)*10)
        with self.assertRaises(BudgetDenied): self.pack(provider=counter)

    def chain(self, counter=None):
        from langchain_core.chat_history import InMemoryChatMessageHistory
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
        from langchain_core.runnables import RunnableLambda
        from rag.pipeline import build_history_chain
        self.generated = []
        def generate(prompt, max_output_tokens):
            self.generated.append((prompt.to_messages(), max_output_tokens))
            return AIMessage(content='42 [S1]', usage_metadata={'input_tokens':100,'output_tokens':5,'total_tokens':105})
        prompt = ChatPromptTemplate.from_messages([('system','Rules {response_mode}\n{context}'),
                    MessagesPlaceholder('chat_history'), ('human','{input}')])
        history = InMemoryChatMessageHistory()
        return build_history_chain(RunnableLambda(lambda _: [doc('42'),doc('X'*30000,source='big')]),
                    RunnableLambda(generate), prompt, lambda _: history, model_name='test-model',
                    token_counter=counter or self.counter)

    def test_pipeline_generates_exact_counted_payload_and_selected_sources(self):
        counted = []
        def count(messages):
            counted.append(messages)
            return exact_test_counter(messages)
        chain = self.chain(TokenCounter(self.contract, count))
        with patch('rag.token_budget.write_event'):
            result = chain.invoke({'input':'Q','mode':'expert'},config={'configurable':{'session_id':'one'}})
        self.assertEqual(tuple(self.generated[0][0]), counted[-1])
        self.assertEqual(self.generated[0][1], 3000)
        self.assertEqual(len(result['sources']), 1)

    def test_pipeline_oversized_question_never_generates(self):
        chain = self.chain(TokenCounter(replace(self.contract,input_limit=3000),exact_test_counter))
        with self.assertRaises(BudgetDenied):
            chain.invoke({'input':'Ж'*7000},config={'configurable':{'session_id':'one'}})
        self.assertEqual(self.generated, [])

    def test_external_revoke_during_count_stops_packing(self):
        import json
        import tempfile
        from pathlib import Path
        from rag.privacy import PolicyStore, PrivacyGate, PrivacyDenied
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'policy.json'
            policy = {'schema_version':1,'topics':{'test':{'external_generation':'allow',
                      'providers':{'gemini':['https://example.com']}}}}
            path.write_text(json.dumps(policy))
            gate = PrivacyGate(PolicyStore(path),'test','gemini','https://example.com')
            lease = gate.authorize(['a'])
            def revoke(messages):
                policy['topics']['test']['external_generation'] = 'deny'
                path.write_text(json.dumps(policy))
                return exact_test_counter(messages)
            counter = TokenCounter(replace(self.contract,external=True,provider='gemini',
                                           endpoint='https://example.com'),revoke)
            spy = Mock(return_value=1)
            with self.assertRaises(BudgetDenied):
                self.pack(provider=TokenCounter(counter.contract,spy),
                          lease=gate.authorize(['unrelated']),privacy_gate=gate)
            spy.assert_not_called()
            with self.assertRaises(PrivacyDenied):
                self.pack(provider=counter, lease=lease, privacy_gate=gate)

    def test_history_is_sacrificed_before_evidence(self):
        docs = [doc('42'),doc('alternative',source='other')]
        baseline = pack_request('S','Q',[],docs,'text',self.counter)
        small = TokenCounter(replace(self.contract,input_limit=baseline.input_tokens+276), exact_test_counter)
        result = pack_request('S','Q',[HumanMessage(content='h'*100),AIMessage(content='a'*100)],
                              docs,'text',small)
        self.assertEqual(result.history, ())
        self.assertEqual(len(result.evidence), 2)

    def test_untrusted_internal_fields_cannot_bypass_packing(self):
        chain = self.chain(TokenCounter(replace(self.contract,input_limit=3000),exact_test_counter))
        with self.assertRaises(BudgetDenied):
            chain.invoke({'input':'Ж'*7000, '_packed':object()},
                         config={'configurable':{'session_id':'one'}})
        self.assertEqual(self.generated, [])

    def test_production_requires_counter_and_model_must_match(self):
        with self.assertRaises(BudgetDenied):
            self.chain(TokenCounter(replace(self.contract,model='another-model'),exact_test_counter))
        from rag.pipeline import build_history_chain
        with patch.dict('os.environ', {'RAG_ENV':'production'}):
            with self.assertRaises(BudgetDenied):
                build_history_chain(None,None,None,None,privacy_gate=object())

    def test_legacy_path_ignores_request_internal_fields(self):
        from langchain_core.chat_history import InMemoryChatMessageHistory
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.runnables import RunnableLambda
        from rag.pipeline import build_history_chain
        chain = build_history_chain(RunnableLambda(lambda _: [doc('42')]),
                    RunnableLambda(lambda _: AIMessage(content='42 [S1]')),
                    ChatPromptTemplate.from_messages([('human','{input} {context}')]),
                    lambda _: InMemoryChatMessageHistory())
        result = chain.invoke({'input':'Q','_packed':object(),'_policy_lease':object()},
                              config={'configurable':{'session_id':'one'}})
        self.assertIn('42', result['answer'])


if __name__ == '__main__': unittest.main()
