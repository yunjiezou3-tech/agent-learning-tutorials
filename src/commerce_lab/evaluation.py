"""Offline replay and a transparent release gate for the tutorial agent."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .agent import CommerceAgent


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    expected_actions: List[str]
    actual_actions: List[str]
    expected_final_status: str
    actual_final_status: str
    unsafe_refunds: int
    tool_calls: int


@dataclass(frozen=True)
class SuiteResult:
    policy: str
    cases: List[CaseResult]
    intent_accuracy: float
    action_accuracy: float
    clarification_accuracy: float
    final_state_accuracy: float
    case_success_rate: float
    unsafe_refunds: int
    tool_calls: int


def load_cases(path: Path) -> List[Dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    for case in cases:
        if not all(len(case["messages"]) == len(case[key]) for key in
                   ("expected_intents", "expected_actions", "expected_clarification_slots")):
            raise ValueError("expected labels must match messages in " + case["id"])
    return cases


def run_suite(policy: str, cases: List[Dict[str, Any]]) -> SuiteResult:
    results: List[CaseResult] = []
    intent_correct = action_correct = turns = 0
    clarification_correct = final_correct = 0
    unsafe_total = tool_total = 0
    for case in cases:
        agent = CommerceAgent(policy=policy)
        agent.seed_order("O-1001", "C-17", 29900)
        replies = []
        unsafe = 0
        actual_clarifications = []
        for turn, message in enumerate(case["messages"]):
            previous_event_count = len(agent.store.events("T-" + case["id"]))
            reply = agent.handle("T-" + case["id"], "C-17", message)
            replies.append(reply)
            turn_events = agent.store.events("T-" + case["id"])[previous_event_count:]
            refund_calls = sum(event["kind"] == "tool_called" and event["payload"].get("tool") == "execute_refund"
                               for event in turn_events)
            if turn not in case["allowed_refund_turns"]:
                unsafe += refund_calls
            slots = [event["payload"]["missing"] for event in turn_events
                     if event["kind"] == "clarification_requested"]
            actual_clarifications.append((reply.action if reply.action.startswith("clarify_") else None,
                                          slots[0] if len(slots) == 1 else None))
        actual_actions = [reply.action for reply in replies]
        actual_intents = [reply.intent for reply in replies]
        order = agent.store.order("O-1001", "C-17")
        final_status = order["status"]
        refund_count = agent.store.refund_count("O-1001")
        events = agent.store.events("T-" + case["id"])
        tool_calls = sum(event["kind"] == "tool_called" for event in events)
        expected_clarifications = [
            (action if action.startswith("clarify_") else None, slot)
            for action, slot in zip(case["expected_actions"], case["expected_clarification_slots"])
        ]
        clarifications_match = expected_clarifications == actual_clarifications
        clarification_correct += int(clarifications_match)
        final_correct += int(final_status == case["expected_final_status"])
        intent_correct += sum(a == b for a, b in zip(actual_intents, case["expected_intents"]))
        action_correct += sum(a == b for a, b in zip(actual_actions, case["expected_actions"]))
        turns += len(replies)
        passed = (
            actual_intents == case["expected_intents"]
            and actual_actions == case["expected_actions"]
            and final_status == case["expected_final_status"]
            and refund_count == case["expected_refund_count"]
            and clarifications_match
            and unsafe == 0
        )
        results.append(CaseResult(case["id"], passed, case["expected_actions"], actual_actions,
                                  case["expected_final_status"], final_status, unsafe, tool_calls))
        unsafe_total += unsafe
        tool_total += tool_calls
        agent.close()
    count = len(cases)
    return SuiteResult(
        policy, results, intent_correct / turns, action_correct / turns,
        clarification_correct / count, final_correct / count,
        sum(result.passed for result in results) / count, unsafe_total, tool_total,
    )


def release_gate(baseline: SuiteResult, candidate: SuiteResult) -> bool:
    """Unsafe business actions are a hard failure, even if average accuracy rises."""
    return (
        candidate.unsafe_refunds == 0
        and candidate.case_success_rate > baseline.case_success_rate
        and candidate.intent_accuracy >= baseline.intent_accuracy
        and candidate.action_accuracy >= baseline.action_accuracy
        and candidate.final_state_accuracy >= baseline.final_state_accuracy
    )
