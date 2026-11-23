"""
MCP 服务冒烟测试：经由 stdio 拉起 agent/server.py（使用正式题库、临时学习记录），按真实会话的顺序调用全部工具，
打印关键字段并断言基本行为。知识点从题库里题最多的单元动态选择，不依赖具体数据。

用法:
    python -I agent/smoke_test.py <临时学习记录库路径>
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))


def text_of(result):
    """
    取工具返回的文本并解析为 JSON（紧凑知识点表等纯文本原样返回）。

    参数:
        result: call_tool 的返回值
    返回:
        对象或 str
    """
    text = result.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def run(learner_db):
    """
    依次调用各工具并检查。

    参数:
        learner_db: 临时学习记录库路径
    """
    params = StdioServerParameters(
        command=sys.executable, args=["-I", os.path.join(AGENT_DIR, "server.py")],
        env=dict({"KAKOMON_LEARNER_DB": learner_db, "KAKOMON_PROFILE": learner_db + ".profile.md",
                  "KAKOMON_VIEWER_DIR": learner_db + ".viewer", "KAKOMON_VIEWER_PORT": "18767",
                  "KAKOMON_SOLUTIONS": learner_db + ".solutions",
                  "PATH": os.environ.get("PATH", "")},
                 **{k: os.environ[k] for k in ("KAKOMON_DB", "KAKOMON_PAGES") if k in os.environ}))
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as s:
            await s.initialize()

            async def call(name, args=None):
                return text_of(await s.call_tool(name, args or {}))

            tools = sorted(t.name for t in (await s.list_tools()).tools)
            print("工具", len(tools), tools)
            ov = await call("bank_overview")
            print("题库：", ov["problems"], "道大题，有解答", ov["with_solution"], "；科目", ov["subjects"][:5])
            compact = await call("list_taxonomy", {"compact": True})
            print("紧凑知识点表", len(compact), "字")
            tax = await call("list_taxonomy")
            unit = max((u for sub in tax for u in sub["units"]), key=lambda u: u["n"])
            print("题最多的单元：", unit["unit_id"], unit["unit"], unit["n"])
            for kw in ("固有値", "C语言", "eigenvalue", "デッドロック"):
                print(f"搜索“{kw}”：", [c["concept_id"] for c in await call("search_concepts", {"keyword": kw})][:4])
            fp = await call("find_problems", {"concepts": [unit["unit_id"]], "limit": 3})
            print("按知识点找题：", fp["total"], [(r["problem_id"], r["matched_by"]) for r in fp["results"]])
            assert fp["total"] > 0
            kw = await call("find_problems", {"keywords": ["固有値", "積分", "特征值"], "limit": 3})
            print("按关键词找题：", kw["total"], [(r["problem_id"], r["matched_by"], r["snippet"][:30]) for r in kw["results"]])
            tr = await call("exam_trends", {"university": "东科大", "subjects": ["数学"], "top_n": 5})
            print("出题趋势：", tr["scope"]["years"], [(c["concept_id"], c["years"]) for c in tr["concepts"]])
            assert tr["concepts"]
            pid0 = fp["results"][0]["problem_id"]
            sim = await call("similar_problems", {"problem_id": pid0, "limit": 3})
            print("相似题：", sim["total"], [(r["problem_id"], r["score"]) for r in sim["results"]])
            assert pid0 not in [r["problem_id"] for r in sim["results"]]
            assert (await call("show_problems", {"problem_ids": [pid0, "不存在"]}))["missing"] == ["不存在"]
            sv = await call("save_solution", {"problem_id": pid0, "markdown": "测试解答 $x=1$", "verified": "测试"})
            gp = await call("get_problem", {"problem_id": pid0, "include_solution": True})
            assert sv["saved"] and "ai" in gp["solutions"] and gp["solution"], gp.get("solutions")
            print("解答存档：", sv["file"], gp["solutions"], gp["solution"]["kind"])

            q = await call("next_question", {"concepts": [unit["unit_id"]]})
            assert q["found"], q
            print("出题：", q["problem_id"], q["source"]["university"], q["source"]["admission_year"],
                  [f["label"] for f in q["focus"]], q["pool"], "层级", q["level"], "前置", q["prerequisite_labels"])
            labels = [f["label"] for f in q["focus"]]
            results = {lab: ("会" if i == 0 else "不会") for i, lab in enumerate(labels)}
            r = await call("record_attempt", {"problem_id": q["problem_id"], "results": results, "hints": 1,
                                              "error_type": "计算错误", "note": "测试"})
            assert r["recorded"], r
            print("记录：", r["subquestions"], r["mastery_changes"][:2])
            for _ in range(3):
                q2 = await call("next_question", {"concepts": [unit["unit_id"]]})
                if not q2["found"]:
                    print("没有题：", q2["reason"])
                    break
                print("出题：", q2["problem_id"], q2["pool"], "层级", q2["level"], q2.get("expansion"))
                r = await call("record_attempt", {"problem_id": q2["problem_id"], "result": "会", "verified": True})
                assert r["recorded"], r
            bad = await call("record_attempt", {"problem_id": q["problem_id"], "result": "大概会", "subq_labels": labels})
            assert not bad["recorded"]
            sp = await call("serve_problem", {"problem_id": fp["results"][0]["problem_id"]})
            assert sp["found"]
            r = await call("record_attempt", {"problem_id": sp["problem_id"], "result": "跳过", "skip_reason": "没时间"})
            assert r["recorded"], r
            print("指定出题并跳过：", sp["problem_id"], [x["label"] for x in r["subquestions"]])

            assert not (await call("set_goal", {"exam_date": "明年8月", "universities": ["东大"]}))["saved"]
            g = await call("set_goal", {"exam_date": "2027-08", "universities": ["东大"], "programs": ["计算机"]})
            print("目标：", g)
            print("计划：", [(p["concept_id"], p["priority"]) for p in (await call("study_plan"))["plan"][:3]])
            print("进度：", (await call("get_progress"))["by_type"][:3])
            print("复习队列：", (await call("review_queue"))["count"])
            rep = await call("progress_report", {"days": 7})
            print("周报：", rep["attempts"], rep["results"], rep["habits"])
            await call("note_observation", {"text": "喜欢先看提示（测试）", "source": "student"})
            prof = await call("get_profile")
            print("画像：", [f["text"] for f in prof["facts"]], "观察", len(prof["observations"]), "需更新", prof["stale"])
            print("保存画像：", (await call("save_profile", {"markdown": "# 学习画像\n\n- 测试\n"}))["saved"])
            print("报错：", await call("report_issue", {"problem_id": q["problem_id"], "description": "测试"}))
            print("全部检查通过")


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
