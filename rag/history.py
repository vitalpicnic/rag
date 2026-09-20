"""Bound dialogue memory locally; token counts are estimates, not provider billing."""
import math
from langchain_core.chat_history import InMemoryChatMessageHistory


def estimated_tokens(messages):
    return sum(math.ceil(len(str(m.content).encode('utf-8')) / 4) + 6 for m in messages)


def trim_history(messages, budget=800):
    if budget < 16:
        return []
    groups = []
    for message in messages:
        if message.type == 'human':
            groups.append([message])
        elif message.type == 'ai' and groups and len(groups[-1]) == 1:
            groups[-1].append(message)
    kept, used = [], 0
    for group in reversed(groups):
        size = estimated_tokens(group)
        if used + size > budget:
            if not kept:
                allowance = max(0, (budget // len(group) - 6) * 4)
                kept = [[message.model_copy(update={'content': str(message.content).encode('utf-8')[:allowance].decode('utf-8', errors='ignore')})
                         for message in group]]
            break
        kept.insert(0, group)
        used += size
    return [message for group in kept for message in group]


class BoundedHistory(InMemoryChatMessageHistory):
    max_tokens: int = 800

    def add_messages(self, messages):
        self.messages = trim_history([*self.messages, *messages], self.max_tokens)

    def add_message(self, message):
        self.add_messages([message])
