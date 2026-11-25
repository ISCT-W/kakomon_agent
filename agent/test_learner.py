"""
学习助手的多日模拟测试：用一个小的合成题库（与真实题库同结构）和固定时间戳模拟两天的学习，逐项断言：
分层出题与自动扩展、不立即重出刚做过的题、到期复习与提前复习、按小问记录（提示折扣、自报偏差、默认重点）、
指定出题、报错暂停、重复题跳过、分层找题与别名、掌握度衰减、题型统计、学习数据摘要与画像过期、学习计划与报告。

使用临时题库、临时学习记录库与临时画像文件，不碰正式数据。

用法:
    python -I agent/test_learner.py <临时目录>
"""
import json
import os
import random
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(AGENT_DIR), "pipeline"))
import db  # noqa: E402

CONCEPTS = [
    ("la.eigen.compute", "la", "线性代数", "la.eigen", "特征值与对角化", "特征值与特征向量的计算"),
    ("la.eigen.diag", "la", "线性代数", "la.eigen", "特征值与对角化", "可对角化判定与对角化"),
    ("la.eigen.power", "la", "线性代数", "la.eigen", "特征值与对角化", "矩阵的幂与矩阵函数"),
    ("la.eigen.jordan", "la", "线性代数", "la.eigen", "特征值与对角化", "Jordan 标准形"),
    ("la.matrix.det", "la", "线性代数", "la.matrix", "矩阵与行列式", "行列式的计算与性质"),
    ("la.matrix.inverse", "la", "线性代数", "la.matrix", "矩阵与行列式", "逆矩阵的计算与存在条件"),
    ("calc.mint.change", "calc", "微积分", "calc.mint", "重积分", "变量替换与 Jacobian"),
    ("calc.mint.special", "calc", "微积分", "calc.mint", "重积分", "高斯积分、Gamma 函数与 Beta 函数"),
    ("algo.design.dp", "algo", "算法与数据结构", "algo.design", "算法设计", "动态规划"),
    ("algo.ds.tree", "algo", "算法与数据结构", "algo.ds", "数据结构", "二叉搜索树与平衡树"),
    ("prog.coding.c", "prog", "程序设计与语言", "prog.coding", "程序设计", "C 语言"),
]
DOCS = [("DK", "京大/a.pdf", "京都大学", "通信情報システム専攻", 2019),
        ("DT", "东大/b.pdf", "東京大学", "コンピュータ科学専攻", 2020),
        ("DS", "筑波/c.pdf", "筑波大学", "コンピュータサイエンス専攻", 2018)]
# (problem_id, doc, label, est_minutes, content, [(label, key, summary, type, difficulty, primary, [secondary], depends)])
PROBLEMS = [
    ("DK-A1", "DK", "A-1", 40, "極座標変換と行列 A の固有値を求めよ。", [
        ("(1)(a)", "1-a", "求极坐标变换的 Jacobian", "calculation", 1, "calc.mint.change", [], []),
        ("(1)(b)", "1-b", "计算高斯积分", "calculation", 2, "calc.mint.special", ["calc.mint.change"], ["1-a"]),
        ("(2)(a)", "2-a", "求 2 阶矩阵的特征值", "calculation", 1, "la.eigen.compute", [], []),
        ("(2)(b)", "2-b", "求矩阵的 n 次幂", "calculation", 2, "la.eigen.power", ["la.eigen.diag"], ["2-a"])]),
    ("DS-1", "DS", "問題 I", 30, "行列の固有値と対角化可能性を示せ。", [
        ("(1)", "1", "求特征值与特征向量", "calculation", 2, "la.eigen.compute", [], []),
        ("(2)", "2", "证明矩阵可对角化", "proof", 3, "la.eigen.diag", [], ["1"])]),
    ("DT-1", "DT", "問題1", 25, "行列式と逆行列を計算せよ。", [
        ("(1)", "1", "计算行列式", "calculation", 1, "la.matrix.det", [], []),
        ("(2)", "2", "求逆矩阵", "calculation", 2, "la.matrix.inverse", ["la.eigen.compute"], [])]),
    ("DT-2", "DT", "問題2", 35, "Jordan 標準形を求めよ。", [
        ("(1)", "1", "求 Jordan 标准形", "proof", 4, "la.eigen.jordan", [], [])]),
    ("DK-A2", "DK", "A-2", 45, "動的計画法と二分探索木に関する問題。", [
        ("(1)", "1", "写出动态规划算法", "programming", 3, "algo.design.dp", [], []),
        ("(2)", "2", "追踪二叉搜索树的插入", "code_reading", 2, "algo.ds.tree", [], [])]),
    ("DT-3", "DT", "問題3", 20, "対角化せよ。", [
        ("(1)", "1", "对角化 3 阶矩阵", "calculation", 2, "la.eigen.diag", ["la.eigen.compute"], [])]),
]


def build_fixture(path):
    """
    写一个与真实题库同结构的小题库。

    参数:
        path: 题库路径
    """
    con = sqlite3.connect(path)
    con.executescript(db.SCHEMA_SQL)
    con.executemany("INSERT INTO concepts VALUES (?,?,?,?,?,?)", CONCEPTS)
    for doc_id, rel, uni, prog, year in DOCS:
        con.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, rel, uni, "研究科", prog, year, "", "専門", "specialized", "ja", "exam_paper", "", prog))
    years = {d[0]: d[4] for d in DOCS}
    dup = [("DT-9", "DT", "問題9", 30, PROBLEMS[1][4], PROBLEMS[1][5])]
    for n, (pid, doc, label, est, content, subs) in enumerate(PROBLEMS + dup, 1):
        con.execute("INSERT INTO problems VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, doc, n, label, label, "", years[doc], "文档", f"题目{n}", content, "", "", "", "[1]",
                     max(s[4] for s in subs), est, "", "DS-1" if pid == "DT-9" else ""))
        con.execute("INSERT INTO problems_fts VALUES (?,?,?,?)", (pid, f"题目{n}", content, " ".join(s[2] for s in subs)))
        for k, (slabel, skey, summ, qtype, diff, prim, secs, deps) in enumerate(subs, 1):
            sid = f"{pid}-{skey}"
            con.execute("INSERT INTO subquestions VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (sid, pid, k, slabel, skey, summ, "要点", json.dumps(deps), qtype, diff))
            con.execute("INSERT INTO subq_concepts VALUES (?,?,?)", (sid, prim, "primary"))
            for c in secs:
                con.execute("INSERT INTO subq_concepts VALUES (?,?,?)", (sid, c, "secondary"))
    con.commit()
    con.close()


def main():
    """
    运行模拟并逐项断言。
    """
    tmp = sys.argv[1]
    os.makedirs(tmp, exist_ok=True)
    for name in ("bank_sim.sqlite", "learner_sim.sqlite", "profile_sim.md"):
        if os.path.exists(os.path.join(tmp, name)):
            os.remove(os.path.join(tmp, name))
    os.environ["KAKOMON_PROFILE"] = os.path.join(tmp, "profile_sim.md")
    os.environ["KAKOMON_SOLUTIONS"] = os.path.join(tmp, "solutions")
    shutil.rmtree(os.environ["KAKOMON_SOLUTIONS"], ignore_errors=True)
    build_fixture(os.path.join(tmp, "bank_sim.sqlite"))
    import bank
    import insights
    import learner
    import mastery
    import tutor
    bcon, lcon = bank.connect(os.path.join(tmp, "bank_sim.sqlite")), learner.connect(os.path.join(tmp, "learner_sim.sqlite"))
    rng = random.Random(0)
    t = datetime(2026, 11, 10, 20, 0)

    def ask(concepts, mode="adaptive", **kw):
        q = tutor.next_question(bcon, lcon, concepts, mode=mode, at=t, rng=rng, **kw)
        if q["found"]:
            print(f"  [{t:%m-%d %H:%M}] 出题 {q['problem_id']} 重点 {[f['label'] for f in q['focus']]} 层级 {q['level']} "
                  f"来源 {q['pool']} 预测 {q['predicted']}" + (f" 扩展到 {[x['concept_id'] for x in q['expansion']['to']]}"
                                                            if q["expansion"] else ""))
        else:
            print(f"  [{t:%m-%d %H:%M}] 没有题：{q['reason']}")
        return q

    def rec(pid, **kw):
        r = tutor.record_attempt(bcon, lcon, pid, at=t, **kw)
        assert r["recorded"], r
        print("    记录", [(s["label"], s["result"], s["score"]) for s in r["subquestions"]],
              [(c["concept_id"], c["from"], c["to"]) for c in r["mastery_changes"]][:3])
        return r

    print("第 1 天：同一知识点从主知识点扩展到次知识点、相邻知识点")
    q = ask(["la.eigen.compute"])
    assert q["problem_id"] == "DK-A1" and q["level"] == 1 and [f["label"] for f in q["focus"]] == ["(2)(a)"], "先出最简单的主知识点题"
    t += timedelta(minutes=10)
    r = rec("DK-A1", results={"(2)(a)": "会"}, verified=True)
    assert r["minutes"] == 10.0 and r["minutes_source"] == "估计", "没报用时时按出题到记录的时间估计"
    t += timedelta(minutes=10)
    q = ask(["la.eigen.compute"])
    assert q["problem_id"] == "DS-1" and q["level"] == 1 and q["prerequisite_labels"] == [], q
    t += timedelta(minutes=15)
    r = rec("DS-1", result="部分会", hints=1, error_type="概念不清", note="不会求特征向量")
    assert r["subquestions"][0]["score"] == 0.375, "部分会且看过 1 次提示：0.5 × 0.75"
    t += timedelta(minutes=5)
    q = ask(["la.eigen.compute"])
    assert q["level"] == 2 and q["expansion"] and q["problem_id"] in ("DT-1", "DT-3"), "主知识点出完后扩展到次知识点"
    first_l2 = q["problem_id"]
    t += timedelta(minutes=10)
    rec(first_l2, result="部分会", self_report="会", verified=True, minutes=8)
    t += timedelta(minutes=5)
    q = ask(["la.eigen.compute"])
    assert q["level"] == 2 and q["problem_id"] in ("DT-1", "DT-3") and q["problem_id"] != first_l2
    t += timedelta(minutes=10)
    rec(q["problem_id"], result="会", verified=True)
    t += timedelta(minutes=5)
    q = ask(["la.eigen.compute"])
    assert q["level"] == 3 and q["problem_id"] == "DT-2", "次知识点也出完后扩展到相邻知识点"
    t += timedelta(minutes=5)
    rec("DT-2", result="跳过", skip_reason="太难")
    t += timedelta(minutes=5)
    q = ask(["la.eigen.compute"])
    assert not q["found"], "刚做过的题（不到 20 小时）不会马上重出"

    print("指定出题、默认重点与按上级编号展开")
    q = tutor.serve_problem(bcon, lcon, "DK-A2", ["(1)"], at=t)
    assert q["found"] and [f["label"] for f in q["focus"]] == ["(1)"]
    t += timedelta(minutes=20)
    r = rec("DK-A2", result="不会", error_type="没思路")
    assert [s["label"] for s in r["subquestions"]] == ["(1)"], "沿用最近一次出题的重点小问"
    bad = tutor.record_attempt(bcon, lcon, "DK-A1", result="会", at=t)
    assert not bad["recorded"], "不是最近一次出的题，必须写明小问"
    r = rec("DK-A1", results={"(1)": "会"}, hints=2)
    assert [s["label"] for s in r["subquestions"]] == ["(1)(a)", "(1)(b)"] and r["subquestions"][0]["score"] == 0.6
    r = rec("DK-A1", results={"2(b)": "不会"}, solution_viewed=True)
    assert r["subquestions"][0]["score"] == 0.0

    print("第 2 天：到期复习、提前复习、报错暂停")
    t = datetime(2026, 11, 11, 21, 0)
    q = ask(["la.eigen.compute"], mode="review")
    assert q["found"] and q["pool"] == "到期复习", "第 2 天有到期的题"
    t += timedelta(minutes=10)
    rec(q["problem_id"], result="会", verified=True)
    t += timedelta(minutes=5)
    seen = set()
    for _ in range(3):
        q = ask(["la.eigen.compute"])
        if not q["found"]:
            break
        assert q["pool"] in ("到期复习", "回炉（之前不会或部分会的题）", "提前复习（相关的新题都做完了）"), q["pool"]
        seen.add(q["problem_id"])
        t += timedelta(minutes=10)
        rec(q["problem_id"], result="会", verified=True)
    assert seen, "新题出完后会回炉或提前复习"
    learner.report_issue(lcon, "DT-3", "公式有误（测试）", at=t)
    q = tutor.next_question(bcon, lcon, ["la.eigen.diag"], allow_seen=True, at=t, rng=rng)
    assert q.get("problem_id") != "DT-3", "有未处理报错的题暂停出题"

    print("重复题")
    assert "DT-9" not in learner.served_problems(lcon), "重复题不会被出"
    assert bank.problem_detail(bcon, "DS-1")["also_in"] and bank.problem_detail(bcon, "DT-9")["duplicate_of"] == "DS-1"
    n_compute = next(c["n"] for c in bank.search_concepts(bcon, "la.eigen.compute") if c["concept_id"] == "la.eigen.compute")
    assert n_compute == 2, "知识点题量不计重复题"
    assert bank.overview(bcon)["duplicates_skipped"] == 1

    print("找题与别名")
    assert {c["concept_id"] for c in bank.search_concepts(bcon, "固有値")} >= {"la.eigen.compute"}
    assert any(c["concept_id"] == "prog.coding.c" for c in bank.search_concepts(bcon, "C语言")), "忽略空格匹配"
    assert any(c["concept_id"] == "algo.ds.tree" for c in bank.search_concepts(bcon, "二叉树")), "别名匹配"
    rows, total = bank.find_problems(bcon, keywords=["固有値"])
    assert {r["problem_id"] for r in rows} == {"DK-A1", "DS-1"}, "日文关键词召回原文"
    rows, total = bank.find_problems(bcon, keywords=["特征值"])
    assert "DS-1" in {r["problem_id"] for r in rows}, "中文关键词召回小问摘要"
    rows, total = bank.find_problems(bcon, concepts=["la.eigen"], question_type="proof")
    assert {r["problem_id"] for r in rows} == {"DS-1", "DT-2"}, "按题型过滤"
    assert "la.eigen.diag" in bank.neighbors(bcon, ["la.eigen.compute"])
    assert bank.resolve_university("Science Tokyo") == "東京工業大学" and "コンピュータ" in bank.resolve_program("计算机")
    assert bank.resolve_universities("七帝大+东科大") == bank.SEVEN_IMPERIAL + ["東京工業大学"]
    rows, total = bank.find_problems(bcon, concepts=["la"], university="七帝大")
    assert {r["source"]["university"] for r in rows} == {"京都大学", "東京大学"}, "学校组过滤（不含筑波）"
    assert {r["university"] for r in bank.candidates(bcon, ["la"], "京大,筑波")} == {"京都大学", "筑波大学"}
    rows, total = bank.find_problems(bcon, concepts=["la.eigen.compute", "la.eigen.diag"])
    assert rows[0]["problem_id"] == "DS-1" and [r["score"] for r in rows] == [4.0, 3.0, 3.0, 1.0], "共同知识点越多越靠前"
    assert rows[0]["subquestions"][0].startswith("(1)｜calculation｜难2｜la.eigen.compute"), "小问压缩成一行"
    assert bank.year_ranges([2003, 2004, 2005, 2007, 0]) == "2003-2005, 2007（另有 1 份年度不明）"
    ov = bank.overview(bcon, "京大")
    assert ov["matched_universities"] == ["京都大学"] and ov["problems"] == 2 and ov["universities"][0]["programs"][0]["papers_list"]
    rows, total = bank.find_problems(bcon, concepts=["la"], brief=True)
    assert rows and "subquestions" not in rows[0], "简要模式不列小问"

    print("出题趋势、相似题、解答存档")
    assert bank.resolve_subjects(bcon, ["数学"])[:2] == ["la", "calc"] and bank.resolve_subjects(bcon, ["线性代数"]) == ["la"]
    tr = bank.exam_trends(bcon, "京大", subjects=["la"])
    assert tr["scope"]["problems"] == 1 and {c["concept_id"] for c in tr["concepts"]} == {"la.eigen.compute", "la.eigen.power"}
    assert all(c["years"] == "2019" for c in tr["concepts"])
    rows, total = bank.similar_problems(bcon, "DS-1")
    assert {r["problem_id"] for r in rows} == {"DK-A1", "DT-3", "DT-1"}, "排除原题和重复题"
    assert [r["score"] for r in rows] == [6.5, 6.5, 2.5], [(r["problem_id"], r["score"]) for r in rows]
    import solutions
    import viewer
    assert solutions.best("DS-1") is None and solutions.available("DS-1") == []
    solutions.save_ai("DS-1", "(1) 特征值为 1、3", ["(1)"], "numpy")
    sol = solutions.save_ai("DS-1", "(2) 可对角化", ["(2)"], append=True)
    assert sol["kind"] == "ai" and sol["covers"] == "(1)、(2)" and "(1)" in sol["text"] and "(2)" in sol["text"]
    assert os.path.exists(os.path.join(tmp, "solutions", "DS-1.ai.md")), "文件名标明 AI"
    with open(os.path.join(tmp, "solutions", "DS-1.human.md"), "w", encoding="utf-8") as f:
        f.write("人工解答")
    assert solutions.available("DS-1") == ["human", "ai"] and solutions.best("DS-1")["kind"] == "human", "人工优先于 AI"
    assert solutions.best("DS-1", "题库解答", "x.pdf")["kind"] == "bank", "题库解答优先"
    assert solutions.best("DS-1", "社区解答", "src/a.md")["kind"] == "human", "社区解答排在人工之后"
    assert solutions.best("DS-2", "社区解答", "src/a.md")["label"].startswith("社区解答"), "没有人工解答时用社区解答"
    assert viewer.item(bank.problem_detail(bcon, "DS-1"), solution=solutions.best("DS-1"))["solution"]["label"] == "人工解答"

    print("掌握度衰减、题型统计、学习数据摘要与画像")
    state = mastery.replay(learner.all_attempts(lcon))
    now = mastery.summary(state, "la.eigen.compute", t)
    later = mastery.summary(state, "la.eigen.compute", t + timedelta(days=90))
    print("  特征值计算 现在", now, "90 天后", later)
    assert later["mastery"] < now["mastery"] and later["mastery_peak"] == now["mastery_peak"], "不练会遗忘，历史最高不变"
    prog = insights.progress(bcon, lcon, at=t)
    types = {x["question_type"]: x for x in prog["by_type"]}
    assert types["proof"]["skips"] == 1 and types["calculation"]["attempts"] >= 5, prog["by_type"]
    payload = insights.profile_payload(bcon, lcon, at=t)
    print("  数据摘要：", [f["text"] for f in payload["facts"]])
    assert payload["stale"] and payload["facts"], "作答够多、还没有画像时提示更新"
    learner.add_observation(lcon, "喜欢先看提示再自己推", "student", at=t)
    learner.save_profile(lcon, "# 画像\n- 测试", payload["attempts"], at=t)
    payload = insights.profile_payload(bcon, lcon, at=t)
    assert not payload["stale"] and payload["observations"][0]["source"] == "student"
    hb = insights.habits(lcon, t)
    assert hb["active_days"] == 2 and hb["sessions"] == 2, hb

    print("学习计划与报告")
    learner.set_goal(lcon, "2027-08-20", ["京都大学"], [], at=t)
    plan = insights.study_plan(bcon, lcon, at=t)
    assert plan["matched_target"] and plan["plan"] and not plan["notice"]
    plan = insights.study_plan(bcon, lcon, ["大阪大学"], at=t)
    assert not plan["matched_target"] and plan["notice"]
    plan = insights.study_plan(bcon, lcon, None, ["通信"], at=t)
    assert plan["matched_target"] and plan["target"] == {"universities": ["京都大学"], "programs": ["通信"]}, "只给专攻时沿用目标学校"
    assert learner.exam_day("2027-08") == date(2027, 8, 1) and learner.exam_day("明年8月") is None
    learner.set_goal(lcon, "2027-08", ["京都大学"], [], at=t)
    assert insights.study_plan(bcon, lcon, at=t)["days_to_exam"] == (date(2027, 8, 1) - t.date()).days, "只知道月份时按 1 日算"
    rep = insights.report(bcon, lcon, 7, at=t)
    assert rep["attempts"] > 0 and rep["by_type"] and rep["habits"]["sessions"] == 2
    print("  周报：", rep["attempts"], rep["results"], "看提示", rep["hinted"], "习惯", rep["habits"])
    print("全部断言通过")


if __name__ == "__main__":
    main()
