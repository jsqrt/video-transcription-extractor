"""Unit tests for stage 3 — hybrid publicistic formatting of the digest.

The formatter (app.services.formatter) adds structure WITHOUT letting the
model touch the prose: it asks the model for a PLAN ONLY (headings,
paragraph indices, recommendations) via constrained JSON, then reassembles
the markdown from the VERBATIM digest paragraphs in code. These tests pin
that contract: every paragraph survives byte-for-byte, the plan's headings
and recommendations are rendered, paragraphs the plan forgot are still
emitted, and a failed plan falls back to the unformatted text.
"""

from __future__ import annotations

import unittest

from app.services.formatter import format_digest
from app.services.tokenization import count_tokens


class _PlanLLM:
    """Fake LLM: records plan prompts, returns a canned JSON plan.

    ``plan`` is the dict returned from ``chat_json``; ``plan_fn`` lets a
    test compute the plan from the user prompt. ``raise_exc`` simulates a
    provider error so the pass-through path can be tested.
    """

    def __init__(self, plan=None, plan_fn=None, raise_exc=None) -> None:
        self.plan = plan
        self.plan_fn = plan_fn
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, system_prompt, user_prompt, *, temperature=0.2,
                  response_schema=None):
        self.calls.append((system_prompt, user_prompt))
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.plan_fn is not None:
            return self.plan_fn(user_prompt)
        return self.plan if self.plan is not None else {"blocks": []}


class FormatDigestTests(unittest.TestCase):
    def test_empty_returns_empty(self):
        llm = _PlanLLM()
        self.assertEqual(format_digest("", llm), "")
        self.assertEqual(format_digest("   \n\n  ", llm), "")
        self.assertEqual(llm.calls, [])

    def test_headings_and_recs_rendered_with_verbatim_paragraphs(self):
        digest = "Подія А у Дніпрі. 20 поранених.\n\nПодія Б у Львові. 5 загиблих."
        plan = {
            "blocks": [
                {
                    "heading": "Удари по містах",
                    "paragraphs": [1, 2],
                    "recommendations": ["Будьте в укритті."],
                }
            ]
        }
        out = format_digest(digest, _PlanLLM(plan=plan), language="uk")
        self.assertIn("## Удари по містах", out)
        # Paragraph prose is verbatim — not rewritten.
        self.assertIn("Подія А у Дніпрі. 20 поранених.", out)
        self.assertIn("Подія Б у Львові. 5 загиблих.", out)
        self.assertIn("### Рекомендації", out)
        self.assertIn("- Будьте в укритті.", out)

    def test_no_fact_dropped_even_if_plan_is_partial(self):
        # The plan only mentions paragraph 1; paragraph 2 must still appear
        # verbatim (the safety net), never silently dropped.
        digest = "Перший факт про подію.\n\nДругий факт про іншу подію."
        plan = {"blocks": [{"heading": "Тема", "paragraphs": [1]}]}
        out = format_digest(digest, _PlanLLM(plan=plan), language="uk")
        self.assertIn("Перший факт про подію.", out)
        self.assertIn("Другий факт про іншу подію.", out)

    def test_every_paragraph_survives_verbatim(self):
        paras = [f"Унікальний факт номер {i} про подію {i}." for i in range(6)]
        digest = "\n\n".join(paras)
        # Plan groups them all into one block.
        plan = {"blocks": [{"heading": "Все разом", "paragraphs": list(range(1, 7))}]}
        out = format_digest(digest, _PlanLLM(plan=plan), language="uk")
        for p in paras:
            self.assertIn(p, out)

    def test_paragraphs_planned_in_windows_by_token_budget(self):
        # Two paragraphs that together exceed a tiny budget are planned in
        # two separate windows → two chat_json calls.
        p1 = "Перша подія сталася у Дніпрі. " * 5
        p2 = "Друга подія сталася у Києві. " * 5
        digest = f"{p1.strip()}\n\n{p2.strip()}"
        budget = count_tokens(p1)  # smaller than p1+p2 together
        llm = _PlanLLM(plan={"blocks": []})
        format_digest(digest, llm, language="uk", target_tokens=budget)
        self.assertEqual(len(llm.calls), 2)

    def test_failed_plan_passes_through_unformatted(self):
        digest = "Важливий факт про подію."
        llm = _PlanLLM(raise_exc=RuntimeError("ollama down"))
        out = format_digest(digest, llm, language="uk")
        self.assertIn("Важливий факт про подію", out)

    def test_model_never_receives_paragraph_text_to_echo_back(self):
        # The user prompt carries numbered paragraphs (so the model can
        # reference them), but the OUTPUT comes from code — proven by the
        # paragraph text surviving even when the model returns an empty
        # plan.
        digest = "Факт із числом 42 у Харкові."
        llm = _PlanLLM(plan={"blocks": []})
        out = format_digest(digest, llm, language="uk")
        self.assertIn("Факт із числом 42 у Харкові.", out)


if __name__ == "__main__":
    unittest.main()
