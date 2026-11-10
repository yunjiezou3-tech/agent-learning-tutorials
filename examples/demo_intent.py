"""Run from the repository root: python examples/demo_intent.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from commerce_lab import CommerceAgent  # noqa: E402


def main() -> None:
    agent = CommerceAgent()
    agent.seed_order("O-1001", "C-17", 29900)
    messages = [
        "这单想退，能退多少钱？",
        "订单 O-1001，帮我退款",
        "确认退款",
    ]
    for turn, message in enumerate(messages, 1):
        reply = agent.handle("T-1001", "C-17", message)
        actual_order = agent.store.order("O-1001", "C-17")
        print("第{}轮 用户：{}".format(turn, message))
        print("     意图={}；动作={}；订单={}；工单={}".format(reply.intent, reply.action, actual_order["status"], reply.ticket_status))
        print("     Agent：{}".format(reply.message))
    print("退款工具实际执行次数：{}".format(agent.store.refund_count("O-1001")))
    agent.close()


if __name__ == "__main__":
    main()
