"""A tiny stateful agent: model interpretation, durable state, guarded tools."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .intent import KeywordBaselineRecognizer, RuleRecognizer
from .storage import LabStore


CLARIFICATION_SLOTS = {"clarify_order": "order_id", "clarify_draft": "draft", "clarify_intent": "intent"}


@dataclass
class TicketState:
    customer_id: Optional[str] = None
    order_id: Optional[str] = None
    last_intent: str = "unknown"
    status: str = "new"
    draft: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class AgentReply:
    intent: str
    action: str
    message: str
    order_id: Optional[str]
    order_status: Optional[str]
    ticket_status: str
    draft_id: Optional[str] = None


class CommerceAgent:
    """The recognizer proposes an intent; this class decides whether tools may run."""

    def __init__(self, db_path: str = ":memory:", policy: str = "guarded_candidate", recognizer=None) -> None:
        if policy not in {"guarded_candidate", "keyword_baseline"}:
            raise ValueError("unknown policy: " + policy)
        self.store = LabStore(db_path)
        self.policy = policy
        self.recognizer = recognizer or (KeywordBaselineRecognizer() if policy == "keyword_baseline" else RuleRecognizer())

    def close(self) -> None:
        self.store.close()

    def seed_order(self, order_id: str = "O-1001", customer_id: str = "C-17", amount_cents: int = 29900) -> None:
        self.store.seed_order(order_id, customer_id, amount_cents)

    def remember_preference(self, customer_id: str, key: str, value: str, source_ticket: str) -> None:
        self.store.remember(customer_id, key, value, source_ticket)
        self.store.append_event(source_ticket, "memory_written", {"customer_id": customer_id, "key": key, "value": value})

    def recall_preference(self, customer_id: str, key: str) -> Optional[Dict[str, str]]:
        return self.store.recall(customer_id, key)

    def ticket_state(self, ticket_id: str) -> TicketState:
        state = TicketState()
        for event in self.store.events(ticket_id):
            payload = event["payload"]
            kind = event["kind"]
            if kind == "ticket_started":
                state.customer_id = payload["customer_id"]
                state.status = "active"
            elif kind == "order_identified":
                state.order_id = payload["order_id"]
            elif kind == "decision":
                state.last_intent = payload["intent"]
            elif kind == "clarification_requested":
                if payload["missing"] == "order_id":
                    state.status = "waiting_order_id"
            elif kind == "refund_drafted":
                state.draft = payload
                state.status = "waiting_confirmation"
            elif kind == "draft_cancelled":
                state.draft = None
                state.status = "active"
            elif kind == "refund_executed":
                state.draft = None
                state.status = "completed"
            elif kind == "handoff_requested":
                state.status = "human_handoff"
        return state

    def context_view(self, ticket_id: str, keep_messages: int = 2) -> Dict[str, Any]:
        """A compact model-facing projection; the full event log is untouched."""
        state = self.ticket_state(ticket_id)
        all_events = self.store.events(ticket_id)
        messages = [event for event in all_events if event["kind"] in {"user_message", "assistant_message"}]
        draft = state.draft
        summary = {
            "ticket_id": ticket_id,
            "order_id": state.order_id,
            "last_intent": state.last_intent,
            "ticket_status": state.status,
            "pending_refund_draft": draft["draft_id"] if draft else None,
        }
        return {
            "summary": summary,
            "recent_messages": messages[-keep_messages:] if keep_messages > 0 else [],
            "full_event_count": len(all_events),
        }

    def handle(self, ticket_id: str, customer_id: str, message: str) -> AgentReply:
        if not message.strip():
            raise ValueError("message must not be empty")
        before = self.ticket_state(ticket_id)
        if before.customer_id is None:
            self.store.append_event(ticket_id, "ticket_started", {"customer_id": customer_id})
        elif before.customer_id != customer_id:
            raise ValueError("ticket belongs to another customer")
        self.store.append_event(ticket_id, "user_message", {"text": message})
        state = self.ticket_state(ticket_id)
        recognized = self.recognizer.recognize(message)
        intent = recognized.label

        if intent in {"partial_refund", "handoff"} and state.draft:
            self.store.append_event(ticket_id, "draft_cancelled", {
                "draft_id": state.draft["draft_id"], "reason": intent,
            })
            state.draft = None
            state.status = "active"

        # The order id is verified against the business system, not trusted from a model.
        if recognized.order_id:
            order = self.store.order(recognized.order_id, customer_id)
            self._tool_event(ticket_id, "get_order", recognized.order_id, "found" if order else "not_found")
            if order is None:
                return self._reply(ticket_id, intent, "clarify_order", "找不到属于你的这个订单，请核对订单号。", state, None)
            if state.draft and recognized.order_id != state.draft["order_id"]:
                return self._reply(ticket_id, intent, "clarify_order", "当前有另一笔待确认退款，请先取消或完成它。", state, state.order_id)
            if recognized.order_id != state.order_id:
                self.store.append_event(ticket_id, "order_identified", {"order_id": recognized.order_id})
                state.order_id = recognized.order_id

        order_id = recognized.order_id or state.order_id
        if intent == "partial_refund":
            self.store.append_event(ticket_id, "handoff_requested", {"reason": "partial_refund"})
            state.status = "human_handoff"
            return self._reply(ticket_id, intent, "handoff", "部分退款需要核对商品与金额，已转人工处理；当前不会退款。", state, order_id)
        if intent == "handoff":
            self.store.append_event(ticket_id, "handoff_requested", {})
            state.status = "human_handoff"
            return self._reply(ticket_id, intent, "handoff", "已转接人工客服，当前不会执行退款。", state, order_id)
        if intent == "cancel":
            if state.draft:
                self.store.append_event(ticket_id, "draft_cancelled", {"draft_id": state.draft["draft_id"]})
                state.draft = None
                state.status = "active"
            return self._reply(ticket_id, intent, "cancel_draft", "已取消待确认退款，没有修改订单。", state, order_id)
        if intent == "confirm":
            return self._confirm(ticket_id, customer_id, state, recognized.order_id)
        if intent in {"quote", "refund", "status"} and not order_id:
            return self._reply(ticket_id, intent, "clarify_order", "请提供订单号，例如 O-1001；我会先核对订单。", state, None)
        if intent == "unknown":
            return self._reply(ticket_id, intent, "clarify_intent", "请说明你想查询退款金额、申请退款，还是转人工。", state, order_id)

        order = self.store.order(order_id, customer_id)
        self._tool_event(ticket_id, "get_order", order_id, "found" if order else "not_found")
        if order is None:
            return self._reply(ticket_id, intent, "clarify_order", "订单未找到，请核对订单号。", state, order_id)
        if intent == "status":
            return self._reply(ticket_id, intent, "read_order", "订单 {} 当前状态：{}。".format(order_id, order["status"]), state, order_id)
        if order["status"] == "refunded":
            return self._reply(ticket_id, intent, "read_order", "订单 {} 已退款，不会重复操作。".format(order_id), state, order_id)

        amount = _money(order["amount_cents"])
        if self.policy == "keyword_baseline":
            # Deliberately bad: a keyword match directly changes business state.
            operation_key = "baseline:{}:{}".format(ticket_id, order_id)
            result = self._refund_tool(ticket_id, order_id, order["version"], operation_key)
            if result in {"executed", "already_applied"}:
                self.store.append_event(ticket_id, "refund_executed", {"order_id": order_id, "operation_key": operation_key})
                state.status = "completed"
                return self._reply(ticket_id, intent, "execute_refund", "已退款 {}。".format(amount), state, order_id)
            return self._reply(ticket_id, intent, "tool_error", "订单状态已变化，退款未执行。", state, order_id)

        if intent == "quote":
            return self._reply(ticket_id, intent, "quote_refund", "订单 {} 预计可退 {}；尚未发起退款。".format(order_id, amount), state, order_id)
        draft_id = "{}:{}:v{}".format(ticket_id, order_id, order["version"])
        draft = {"draft_id": draft_id, "order_id": order_id, "amount_cents": order["amount_cents"], "order_version": order["version"]}
        self.store.append_event(ticket_id, "refund_drafted", draft)
        state.draft = draft
        state.status = "waiting_confirmation"
        return self._reply(ticket_id, intent, "draft_refund", "订单 {} 可退 {}。请回复“确认退款”后执行。".format(order_id, amount), state, order_id)

    def _confirm(self, ticket_id: str, customer_id: str, state: TicketState, supplied_order_id: Optional[str]) -> AgentReply:
        draft = state.draft
        if draft is None:
            return self._reply(ticket_id, "confirm", "clarify_draft", "当前没有待确认的退款草稿，不会执行退款。", state, state.order_id)
        if supplied_order_id and supplied_order_id != draft["order_id"]:
            return self._reply(ticket_id, "confirm", "clarify_draft", "确认的订单与当前退款草稿不一致，未执行退款。", state, draft["order_id"])
        order = self.store.order(draft["order_id"], customer_id)
        self._tool_event(ticket_id, "get_order", draft["order_id"], "found" if order else "not_found")
        if order is None or order["status"] != "paid" or order["version"] != draft["order_version"] or order["amount_cents"] != draft["amount_cents"]:
            self.store.append_event(ticket_id, "draft_cancelled", {"draft_id": draft["draft_id"], "reason": "stale_order"})
            state.draft = None
            state.status = "active"
            return self._reply(ticket_id, "confirm", "stale_draft", "订单或金额已变化，请重新申请退款。", state, draft["order_id"])
        self.store.append_event(ticket_id, "confirmation_received", {"draft_id": draft["draft_id"]})
        result = self._refund_tool(ticket_id, draft["order_id"], draft["order_version"], draft["draft_id"])
        verified = self.store.order(draft["order_id"], customer_id)
        self._tool_event(ticket_id, "get_order", draft["order_id"], "found" if verified else "not_found")
        if result in {"executed", "already_applied"} and verified and verified["status"] == "refunded":
            self.store.append_event(ticket_id, "refund_executed", {"order_id": draft["order_id"], "operation_key": draft["draft_id"]})
            state.draft = None
            state.status = "completed"
            return self._reply(ticket_id, "confirm", "execute_refund", "退款已执行并核验：订单 {} 状态为 refunded。".format(draft["order_id"]), state, draft["order_id"])
        return self._reply(ticket_id, "confirm", "tool_error", "退款结果未核验成功，已停止后续动作。", state, draft["order_id"])

    def _refund_tool(self, ticket_id: str, order_id: str, version: int, operation_key: str) -> str:
        self.store.append_event(ticket_id, "tool_called", {"tool": "execute_refund", "order_id": order_id, "operation_key": operation_key})
        result = self.store.execute_refund(order_id, version, operation_key)
        self.store.append_event(ticket_id, "tool_result", {"tool": "execute_refund", "result": result})
        return result

    def _tool_event(self, ticket_id: str, tool: str, order_id: str, result: str) -> None:
        self.store.append_event(ticket_id, "tool_called", {"tool": tool, "order_id": order_id})
        self.store.append_event(ticket_id, "tool_result", {"tool": tool, "result": result})

    def _reply(self, ticket_id: str, intent: str, action: str, text: str, state: TicketState, order_id: Optional[str]) -> AgentReply:
        if action in CLARIFICATION_SLOTS:
            missing = CLARIFICATION_SLOTS[action]
            self.store.append_event(ticket_id, "clarification_requested", {"missing": missing})
            if missing == "order_id":
                state.status = "waiting_order_id"
        self.store.append_event(ticket_id, "decision", {"intent": intent, "action": action})
        self.store.append_event(ticket_id, "assistant_message", {"text": text})
        order = self.store.order(order_id, state.customer_id) if order_id else None
        return AgentReply(intent, action, text, order_id, order["status"] if order else None, state.status, state.draft["draft_id"] if state.draft else None)


def _money(cents: int) -> str:
    return "¥{:.2f}".format(cents / 100)
