"""In-memory conversation fixtures; no browser transport."""
from __future__ import annotations
from visibility_runner.websites import chat_actions as chat
from visibility_runner.websites.conversation_binding import text_digest


class Item:
    def __init__(self, text, identity):
        self.text, self.identity = text, identity
    async def is_visible(self): return True
    async def inner_text(self): return self.text
    async def get_attribute(self, key): return self.identity if key == "data-message-id" else None


class Locator:
    def __init__(self, items): self.items = items
    async def count(self): return len(self.items)
    def nth(self, index): return self.items[index]


class Page:
    def __init__(self):
        self.url = "about:blank"
        self.users = [Item("measurement", "u1")]
        self.answers = [Item("Alpha Clinic", "a1")]
        self.navigated, self.closed = [], False
    def locator(self, selector):
        return Locator(self.users if selector == "user" else self.answers if selector == "assistant" else [])
    async def goto(self, url, **kwargs): self.url = url; self.navigated.append(url)
    async def close(self): self.closed = True


SPEC = chat.PageProviderSpec(platform="chatgpt", start_url="https://chatgpt.com/",
    composer_selectors=("composer",), send_selectors=("send",), answer_selectors=("assistant",),
    response_node_selectors=("assistant",), user_message_selectors=("user",), stop_selectors=(),
    conversation_url_prefixes=("https://chatgpt.com/c/",), response_timeout_seconds=.5, stable_seconds=.001)


def identity(page, tab="new-owned-tab"):
    return chat.BrowserIdentity("fixture", "Profile 99", "context", tab, "target", page.url)


def binding():
    return {"schema_version": "browser-conversation-binding-v1",
        "identity": {"profile_id": "fixture", "chrome_directory": "Profile 99", "browser_context_reference": "context", "tab_reference": "old-owned-tab", "conversation_reference": "https://chatgpt.com/c/exact-123"},
        "prompt_sha256": text_digest("measurement"), "expected_turn": 1,
        "response_baseline": {"visible_response_count": 0, "newest_response_sha256": text_digest("")},
        "transcript": {"prior_user_sha256": [], "prior_assistant_sha256": [], "user_ids": ["u1"], "assistant_ids": ["a1"]}}
