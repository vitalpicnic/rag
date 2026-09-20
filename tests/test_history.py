from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / '.runtime_deps'), str(ROOT / '.test_deps')]
from langchain_core.messages import HumanMessage, AIMessage
from rag.history import BoundedHistory, estimated_tokens, trim_history


class HistoryTests(unittest.TestCase):
    def test_many_turns_keep_recent_history_under_budget(self):
        history = BoundedHistory(max_tokens=80)
        for i in range(30):
            history.add_messages([HumanMessage(content=f'question {i}'), AIMessage(content='answer')])
        self.assertLessEqual(estimated_tokens(history.messages), 80)
        self.assertEqual(history.messages[-2].content, 'question 29')
        self.assertEqual(history.messages[0].type, 'human')

    def test_oversized_latest_turn_is_shortened_instead_of_losing_topic(self):
        messages = [HumanMessage(content='bank ' * 500), AIMessage(content='number ' * 800)]
        limited = trim_history(messages, 80)
        self.assertEqual([m.type for m in limited], ['human', 'ai'])
        self.assertLessEqual(estimated_tokens(limited), 80)
        self.assertTrue(limited[0].content.startswith('bank'))

    def test_individual_seed_messages_and_small_history_are_preserved(self):
        history = BoundedHistory(max_tokens=80)
        history.add_user_message('question')
        history.add_ai_message('answer')
        self.assertEqual([m.content for m in history.messages], ['question', 'answer'])


if __name__ == '__main__':
    unittest.main()
