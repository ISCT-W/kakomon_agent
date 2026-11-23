"""
生成学习周期报告（Markdown，不调用模型），可由定时任务每周运行。

用法:
    python -I agent/report.py [--days 7] [--out data/reports]
输出:
    data/reports/report_YYYYMMDD.md
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bank  # noqa: E402
import insights  # noqa: E402
import learner  # noqa: E402


def render(rep, plan):
    """
    把报告数据渲染成 Markdown。

    参数:
        rep: insights.report 的结果
        plan: insights.study_plan 的结果
    返回:
        str
    """
    r = rep["results"]
    lines = [f"# 学习报告（{rep['range'][0][:10]} ~ {rep['range'][1][:10]}）", ""]
    if rep["goal"]:
        g = rep["goal"]
        target = "、".join(g["universities"]) or "未指定学校"
        if g["programs"]:
            target += "（" + "、".join(g["programs"]) + "）"
        lines.append(f"- 目标：{target}，考试日期 {g['exam_date']}（还有 {rep['days_to_exam']} 天）")
    h = rep["habits"]
    lines += [f"- 作答 {rep['attempts']} 个小问：会 {r.get('会', 0)}、部分会 {r.get('部分会', 0)}、不会 {r.get('不会', 0)}、"
              f"跳过 {r.get('跳过', 0)}；其中经过批改确认 {rep['verified']} 个，看了提示的 {rep['hinted']} 个",
              f"- 学习 {rep['active_days']} 天、{h['sessions']} 次，平均每次 {h['avg_session_minutes'] or 0} 分钟"
              + (f"，常在 {'、'.join(h['usual_hours'])}" if h["usual_hours"] else "") + f"；连续学习 {h['streak_days']} 天",
              f"- 当前到期待复习：{rep['due_reviews']} 道", ""]
    if rep["subjects"]:
        lines += ["## 各科表现", "", "| 科目 | 作答次数 | 平均得分 |", "|---|---|---|"]
        lines += [f"| {s['subject']} | {s['attempts']} | {s['avg_score']} |" for s in rep["subjects"]]
        lines.append("")
    if rep["by_type"]:
        lines += ["## 各题型表现", "", "| 题型 | 作答 | 平均得分 | 跳过 | 看提示比例 | 用时比 |", "|---|---|---|---|---|---|"]
        def show(v):
            return "—" if v is None else v
        lines += [f"| {t['name']} | {t['attempts']} | {show(t['avg_score'])} | {t['skips']} | {show(t['hint_ratio'])} | "
                  f"{show(t['speed_ratio'])} |" for t in rep["by_type"]]
        lines.append("")
    if rep["mastery_changes"]:
        lines += ["## 掌握度变化", "", "| 知识点 | 期初 | 现在 | 变化 | 等级 |", "|---|---|---|---|---|"]
        lines += [f"| {c['name']}（{c['concept_id']}） | {c['from']} | {c['to']} | {c['delta']:+.2f} | {c['level']} |"
                  for c in rep["mastery_changes"]]
        lines.append("")
    if rep["errors"]:
        lines += ["## 错因分布", ""] + [f"- {k}：{v} 次" for k, v in sorted(rep["errors"].items(), key=lambda x: -x[1])]
        lines.append("")
    if plan["plan"]:
        lines += ["## 接下来建议练习", ""]
        lines += [f"{i}. {p.get('name', '')}（{p['concept_id']}）：" + "；".join(p["reasons"])
                  for i, p in enumerate(plan["plan"], 1)]
        lines.append("")
    if rep["periods"]:
        lines += ["## 重要期间", ""] + [f"- {p['name']}（{p['start_date']} ~ {p['end_date']}，{p['status']}）"
                                      for p in rep["periods"]]
    return "\n".join(lines) + "\n"


def main():
    """
    生成报告文件并打印路径。
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=os.path.join(learner.DATA_DIR, "reports"))
    args = ap.parse_args()
    bank_con, learn_con = bank.connect(), learner.connect()
    md = render(insights.report(bank_con, learn_con, args.days), insights.study_plan(bank_con, learn_con, top_n=5))
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"report_{datetime.now():%Y%m%d}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    print(path)


if __name__ == "__main__":
    main()
