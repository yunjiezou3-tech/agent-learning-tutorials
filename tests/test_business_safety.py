"""Regression checks for the business boundaries used by the lessons."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from commerce_lab.agent import CommerceAgent  # noqa: E402
from commerce_lab.evaluation import run_suite  # noqa: E402
from commerce_lab.intent import RuleRecognizer  # noqa: E402


def case(messages, intents, actions, status="paid", refund_count=0, allowed=None, slots=None):
    return {
        "id": "regression",
        "messages": messages,
        "expected_intents": intents,
        "expected_actions": actions,
        "expected_clarification_slots": slots if slots is not None else [None] * len(messages),
        "expected_final_status": status,
        "expected_refund_count": refund_count,
        "allowed_refund_turns": allowed if allowed is not None else [],
    }


class BusinessSafetyTests(unittest.TestCase):
    def test_unsafe_refund_counts_tool_call_even_when_reply_label_is_masked(self):
        original_reply = CommerceAgent._reply

        def masked_reply(agent, *args, **kwargs):
            reply = original_reply(agent, *args, **kwargs)
            return replace(reply, action="quote_refund") if reply.action == "execute_refund" else reply

        with patch.object(CommerceAgent, "_reply", masked_reply):
            result = run_suite("keyword_baseline", [case(
                ["订单 O-1001，能退多少？"], ["quote"], ["quote_refund"]
            )])
        self.assertEqual(result.unsafe_refunds, 1)
        self.assertEqual(result.cases[0].unsafe_refunds, 1)

    def test_clarification_requires_the_right_slot(self):
        fixture = case(["确认退款"], ["confirm"], ["clarify_draft"], slots=["draft"])
        baseline = run_suite("keyword_baseline", [fixture])
        candidate = run_suite("guarded_candidate", [fixture])
        self.assertEqual(baseline.clarification_accuracy, 0)
        self.assertEqual(candidate.clarification_accuracy, 1)

    def test_partial_refund_needs_human_review_without_refund_call(self):
        self.assertEqual(RuleRecognizer().recognize("订单 O-1001，只退耳机").label, "partial_refund")
        agent = CommerceAgent()
        agent.seed_order()
        try:
            reply = agent.handle("T-partial", "C-17", "订单 O-1001，只退耳机")
            self.assertEqual(reply.action, "handoff")
            self.assertEqual(agent.store.order("O-1001", "C-17")["status"], "paid")
            calls = [e for e in agent.store.events("T-partial")
                     if e["kind"] == "tool_called" and e["payload"]["tool"] == "execute_refund"]
            self.assertEqual(calls, [])
        finally:
            agent.close()

    def test_changed_request_or_handoff_invalidates_old_refund_draft(self):
        for changed_request in ("只退耳机", "转人工客服"):
            with self.subTest(changed_request=changed_request):
                agent = CommerceAgent()
                agent.seed_order()
                try:
                    first = agent.handle("T-change", "C-17", "订单 O-1001，帮我退款")
                    self.assertEqual(first.action, "draft_refund")
                    changed = agent.handle("T-change", "C-17", changed_request)
                    self.assertEqual(changed.action, "handoff")
                    self.assertIsNone(agent.ticket_state("T-change").draft)
                    confirmation = agent.handle("T-change", "C-17", "确认退款")
                    self.assertEqual(confirmation.action, "clarify_draft")
                    self.assertEqual(agent.store.refund_count("O-1001"), 0)
                finally:
                    agent.close()

    def test_changed_request_with_invalid_order_still_invalidates_old_draft(self):
        agent = CommerceAgent()
        agent.seed_order()
        try:
            agent.handle("T-change-order", "C-17", "订单 O-1001，帮我退款")
            changed = agent.handle("T-change-order", "C-17", "订单 O-9999，只退耳机")
            self.assertEqual(changed.action, "clarify_order")
            self.assertIsNone(agent.ticket_state("T-change-order").draft)
            confirmation = agent.handle("T-change-order", "C-17", "确认退款")
            self.assertEqual(confirmation.action, "clarify_draft")
            self.assertEqual(agent.store.refund_count("O-1001"), 0)
        finally:
            agent.close()


if __name__ == "__main__":
    unittest.main()
