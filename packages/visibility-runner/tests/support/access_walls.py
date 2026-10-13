"""Synthetic access-wall nodes and provider specs."""
from __future__ import annotations
from dataclasses import replace


class Node:
    def __init__(self, text="", *, visible=True, descendants=None):
        self.text = text
        self.visible = visible
        self.clicks = []
        self.descendants = descendants or {}

    async def is_visible(self):
        return self.visible

    async def inner_text(self):
        return self.text

    async def fill(self, value):
        self.text = value

    async def input_value(self):
        return self.text

    async def click(self, **kwargs):
        self.clicks.append(kwargs)

    def locator(self, selector):
        return Locator(self.descendants.get(selector, ()))


class Locator:
    def __init__(self, nodes):
        self.nodes = tuple(nodes)

    async def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]

    async def inner_text(self):
        return self.nodes[0].text if self.nodes else ""


class Page:
    def __init__(self, spec, *, body_text="", answer=""):
        self.spec = spec
        self.body_text = body_text
        self.answer = answer
        self.prompt = "synthetic exact measurement"
        self.auth_wall = ()
        self.challenge = ()
        self.unauthenticated_controls = ()
        self.response_error_text = ""
        self.composer = Node()
        self.send = Node()
        self.url = (
            "https://chatgpt.com/c/synthetic"
            if spec.platform == "chatgpt"
            else "https://gemini.google.com/app/synthetic"
        )
        self.browser_context_id = "context-synthetic"
        self.window_name = "tab-synthetic"

    def is_closed(self):
        return False

    async def evaluate(self, script, *_args):
        assert script == "() => window.name || ''"
        return self.window_name

    def locator(self, selector):
        if selector == "body":
            return Locator((Node(self.body_text),))
        if selector == "composer":
            return Locator((self.composer,))
        if selector == "send":
            return Locator((self.send,))
        if selector == "user":
            return Locator((Node(self.prompt),))
        if selector == "response":
            descendants = {
                ", ".join(self.spec.terminal_error_within_response_selectors): (
                    (Node(self.response_error_text),)
                    if self.response_error_text else ()
                )
            }
            if self.spec.response_content_selectors:
                descendants[", ".join(self.spec.response_content_selectors)] = (
                    Node(self.answer),
                )
            return Locator(
                (Node(self.answer, descendants=descendants),)
                if self.answer else ()
            )
        if selector == ", ".join(self.spec.access_wall_selectors):
            return Locator(self.auth_wall)
        if selector == ", ".join(self.spec.human_verification_selectors):
            return Locator(self.challenge)
        if selector == ", ".join(self.spec.unauthenticated_control_selectors):
            return Locator(self.unauthenticated_controls)
        return Locator(())


def local_spec(provider_spec):
    return replace(
        provider_spec,
        composer_selectors=("composer",),
        send_selectors=("send",),
        answer_selectors=("response",),
        response_node_selectors=("response",),
        user_message_selectors=("user",),
        rendered_user_label_selectors=(),
        rendered_answer_label_selectors=(),
        response_timeout_seconds=1.0,
        stable_seconds=0.0,
    )
