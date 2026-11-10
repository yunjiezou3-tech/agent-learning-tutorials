"""Run from the repository root: python examples/demo_continuity.py"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from commerce_lab import CommerceAgent  # noqa: E402


def main() -> None:
    with TemporaryDirectory() as directory:
        db_path = str(Path(directory) / "commerce.sqlite3")
        agent = CommerceAgent(db_path)
        agent.seed_order("O-1001", "C-17", 29900)
        agent.remember_preference("C-17", "notice_channel", "短信", "T-0900")

        first = agent.handle("T-1001", "C-17", "这单想退，能退多少钱？")
        second = agent.handle("T-1001", "C-17", "订单 O-1001，帮我退款")
        view = agent.context_view("T-1001", keep_messages=2)
        print("1. 首轮：{}；动作={}".format(first.message, first.action))
        print("2. 草稿：{}；订单状态={}".format(second.message, second.order_status))
        print("3. 模型可见摘要：{}".format(view["summary"]))
        print("   完整事件仍有 {} 条；只带最近 {} 条消息。".format(view["full_event_count"], len(view["recent_messages"])))
        agent.close()

        # A new process can reconstruct the ticket from the same durable database.
        restarted = CommerceAgent(db_path)
        restored = restarted.ticket_state("T-1001")
        memory = restarted.recall_preference("C-17", "notice_channel")
        print("4. 重启恢复：状态={}；待确认草稿={}".format(restored.status, restored.draft["draft_id"]))
        print("5. 跨会话记忆：通知渠道={}；来源={}".format(memory["memory_value"], memory["source_ticket"]))
        confirmed = restarted.handle("T-1001", "C-17", "确认退款")
        print("6. 用户确认后：{}；订单状态={}".format(confirmed.message, confirmed.order_status))
        restarted.close()


if __name__ == "__main__":
    main()
