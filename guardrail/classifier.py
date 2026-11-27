"""HiveTraceGuard-Pro, a small model that tells whether a text is unsafe.

It answers one word, `safe` or `unsafe`; the probability of `unsafe` is the
result.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, PreTrainedModel

from guardrail.service import ChatMessage

MODEL_REPO = "hivetrace/HiveTraceGuard-Pro"
# Pinned, as the model card advises: another version may change the verdicts
# or the chat template that render_prompt edits.
MODEL_REVISION = "1.1.0"
# The model's two answer words as token ids, from its model card. Their
# logits give the probability that the text is unsafe.
SAFE_TOKEN_ID = 18675
UNSAFE_TOKEN_ID = 38157

# The model's built-in policy also counts content that shows LGBT people in a
# positive light as unsafe, a rule of Russian law rather than an attack on the
# agent. A support bot must not refuse subscribers for that, so the line is
# cut from the policy. The cut lowers the scores of attacks a little: see
# docs/reports/guardrail-engines.md.
DROPPED_POLICY_LINE_START = "LGBT propaganda:"
# The chat template's policy ends where the conversation begins: lines are
# cut only before this marker, so the checked text itself stays whole.
CONVERSATION_START = "<|im_start|>user"


class HiveTraceClassifier:
    """Gives the probability that the last message of a conversation is unsafe.

    The last message is judged in the context of the ones before it: a last
    `user` message as a request, a last `assistant` message as a reply.
    """

    def __init__(self, tokenizer, model, device: str) -> None:
        self.tokenizer = tokenizer
        self.model = model
        self.device = device
        # Whether render_prompt cuts the policy line; the engine comparison
        # script turns it off to measure the full policy.
        self.drops_policy_line = True
        # One forward pass at a time: the service calls this from threads.
        self.lock = threading.Lock()

    @classmethod
    def load(cls) -> HiveTraceClassifier:
        """Load the model, from the local cache or Hugging Face, and warm it up."""
        # A Mac's GPU if there is one: about ten times faster than the CPU.
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        tokenizer = AutoTokenizer.from_pretrained(
            MODEL_REPO, revision=MODEL_REVISION
        )
        model: PreTrainedModel = AutoModelForCausalLM.from_pretrained(
            MODEL_REPO, revision=MODEL_REVISION, dtype=torch.bfloat16
        )
        # transformers wraps `to` in functools.wraps(torch.nn.Module.to), so
        # type checkers read the device as the `self` argument.
        model.to(device).eval()  # pyright: ignore[reportArgumentType]
        classifier = cls(tokenizer, model, device)
        # Warmup model
        # The first pass is slow; let it happen before the first subscriber's
        # turn. It also fails early if the template lost the policy line.
        classifier.find_unsafe_probability([{"role": "user", "content": "Hi"}])
        return classifier

    def render_prompt(self, messages: Sequence[ChatMessage]) -> str:
        """Return the model's prompt for the conversation, policy line cut."""
        prompt: str = self.tokenizer.apply_chat_template(
            list(messages), tokenize=False
        )
        if not self.drops_policy_line:
            return prompt
        policy, conversation = prompt.split(CONVERSATION_START, maxsplit=1)
        policy_lines = policy.split("\n")
        kept_lines = [
            line
            for line in policy_lines
            if not line.startswith(DROPPED_POLICY_LINE_START)
        ]
        if len(kept_lines) != len(policy_lines) - 1:
            raise RuntimeError(
                "the model's chat template changed: the policy line to cut "
                f"({DROPPED_POLICY_LINE_START!r}) isn't there once"
            )
        return "\n".join(kept_lines) + CONVERSATION_START + conversation

    def find_unsafe_probability(self, messages: Sequence[ChatMessage]) -> float:
        """Return the probability, 0 to 1, that the last message is unsafe."""
        prompt = self.render_prompt(messages)
        with self.lock:
            inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)
            with torch.inference_mode():
                # The scores of every possible next token; only the two
                # answer words matter.
                next_token_logits = self.model(**inputs).logits[0, -1]
        answer_logits = next_token_logits[[SAFE_TOKEN_ID, UNSAFE_TOKEN_ID]]
        return torch.softmax(answer_logits.float(), dim=0)[1].item()
