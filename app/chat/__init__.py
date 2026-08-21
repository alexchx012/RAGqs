"""chat-generation domain: conversations, durable generations, SSE, feedback and A/B voting.

This domain is a greenfield rewrite: all conversation, message, generation, event,
feedback and A/B facts start from an empty schema. Compatibility HTTP routes delegate
to these services; they do not add a second conversation or generation implementation.
"""

from .conversations import ConversationService
from .generation import GenerationService
from .streaming import GenerationStreamService
from .worker import ChatGenerationWorker

__all__ = [
    "ChatGenerationWorker",
    "ConversationService",
    "GenerationService",
    "GenerationStreamService",
]
