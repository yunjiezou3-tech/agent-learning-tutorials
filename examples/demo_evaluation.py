"""Run from the repository root: python examples/demo_evaluation.py"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from commerce_lab.evaluation import load_cases, release_gate, run_suite  # noqa: E402


def show(result) -> None:
    print("策略：{}".format(result.policy))
    print("  案例通过率={:.0%} 意图准确率={:.0%} 动作准确率={:.0%} 澄清准确率={:.0%} 最终状态准确率={:.0%}".format(
        result.case_success_rate, result.intent_accuracy, result.action_accuracy,
        result.clarification_accuracy, result.final_state_accuracy,
    ))
    print("  误退款={} 工具调用={}".format(result.unsafe_refunds, result.tool_calls))
    for case in result.cases:
        if not case.passed:
            print("  失败 {}：期望动作={}，实际动作={}，订单={}，误退款={}".format(
                case.case_id, "/".join(case.expected_actions), "/".join(case.actual_actions),
                case.actual_final_status, case.unsafe_refunds,
            ))


def main() -> None:
    cases = load_cases(ROOT / "data" / "eval_cases.json")
    baseline = run_suite("keyword_baseline", cases)
    candidate = run_suite("guarded_candidate", cases)
    print("固定回归集：{} 个售后案例".format(len(cases)))
    show(baseline)
    print("反馈：区分询价与退款，部分退款转人工；整单退款需草稿、明确确认和状态复查。")
    show(candidate)
    print("发布门禁：{}".format("接受候选策略" if release_gate(baseline, candidate) else "拒绝候选策略"))


if __name__ == "__main__":
    main()
