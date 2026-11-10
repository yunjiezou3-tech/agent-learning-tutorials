"""Intent recognition is replaceable; business authorization stays outside the model."""

import json
import re
from dataclasses import dataclass
from typing import Optional
from urllib import request


INTENTS = {"quote", "refund", "partial_refund", "confirm", "cancel", "status", "handoff", "unknown"}
ORDER_ID_PATTERN = re.compile(r"\bO-\d+\b", re.IGNORECASE)


@dataclass(frozen=True)
class IntentResult:
    label: str
    order_id: Optional[str]


def order_id_in(text: str) -> Optional[str]:
    match = ORDER_ID_PATTERN.search(text)
    return match.group(0).upper() if match else None


class RuleRecognizer:
    """A reproducible stand-in for a model during the offline lessons."""

    def recognize(self, text: str) -> IntentResult:
        order_id = order_id_in(text)
        if "先别退" in text or "取消退款" in text:
            label = "cancel"
        elif "人工" in text:
            label = "handoff"
        elif "确认退款" in text:
            label = "confirm"
        elif any(phrase in text for phrase in ("只退", "部分退款", "退其中", "退一件")):
            label = "partial_refund"
        elif any(phrase in text for phrase in ("退多少", "能退多少", "退款金额", "多少钱")):
            label = "quote"
        elif any(phrase in text for phrase in ("订单状态", "订单进度", "查订单")):
            label = "status"
        elif "退" in text:
            label = "refund"
        else:
            label = "unknown"
        return IntentResult(label, order_id)


class KeywordBaselineRecognizer:
    """Intentionally weak baseline: any mention of refund becomes a refund request."""

    def recognize(self, text: str) -> IntentResult:
        if "人工" in text:
            label = "handoff"
        elif "退" in text:
            label = "refund"
        elif "订单" in text:
            label = "status"
        else:
            label = "unknown"
        return IntentResult(label, order_id_in(text))


class OpenAICompatibleRecognizer:
    """Optional real-model adapter; the agent still enforces all action checks."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def recognize(self, text: str) -> IntentResult:
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "只识别用户当前意图。返回 JSON 对象，label 必须是 quote/refund/partial_refund/confirm/cancel/status/handoff/unknown 之一。商品级部分退款归为 partial_refund。不要执行任何业务动作，也不要推断未出现的订单号。"},
                {"role": "user", "content": text},
            ],
        }
        http_request = request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(http_request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        label = json.loads(content).get("label", "unknown")
        if label not in INTENTS:
            label = "unknown"
        # An order id is an action parameter. Never accept one invented by a model.
        return IntentResult(label, order_id_in(text))
