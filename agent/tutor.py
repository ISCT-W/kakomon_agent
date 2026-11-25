"""
刷题核心逻辑：分层出题（含自动扩展）、指定出题、按小问记录作答。

MCP 工具（server.py）只是薄封装；这里的函数都接受 at（本地时间），测试可以用固定时间戳模拟多天的学习。
"""
from datetime import datetime

import bank
import insights
import learner
import mastery
import selector
import solutions

SECONDARY_WEIGHT = 0.4


def present(detail, focus_keys):
    """
    出题时返回给 Claude 的题目内容。

    参数:
        detail: bank.problem_detail 的返回值
        focus_keys: 本次重点小问的规范化键
    返回:
        dict
    """
    subs = {s["key"]: s for s in detail["subquestions"]}
    focus = [subs[k] for k in focus_keys if k in subs]
    needed = [k for s in focus for k in s["depends_on"] if k not in focus_keys]
    return {
        "problem_id": detail["problem_id"], "source": detail["source"], "title_zh": detail["title_zh"],
        "problem_difficulty": detail["difficulty"], "est_minutes": detail["est_minutes"],
        "focus": [{"label": s["label"] or "（整题）", "summary_zh": s["summary_zh"], "difficulty": s["difficulty"],
                   "question_type": s["question_type"],
                   "concept_ids": [c["concept_id"] for c in s["concepts"] if c["role"] == "primary"]} for s in focus],
        "prerequisite_labels": [subs[k]["label"] for k in dict.fromkeys(needed) if k in subs],
        "all_subquestion_labels": [s["label"] or "（整题）" for s in detail["subquestions"]],
        "content_md": detail["content_md"], "errata_md": detail["errata_md"],
        "figures": [{"description": f["description"], "path": f["path"]} for f in detail["figures"] if f["path"]],
        "page_images": detail["page_images"], "solutions": solutions.available(detail["problem_id"], detail["has_solution"]),
        "also_in": detail["also_in"],
    }


def next_question(bcon, lcon, concepts, university="", program="", mode="adaptive", allow_seen=False, expand=True,
                  at=None, rng=None):
    """
    按知识点出下一道题：主知识点 → 次知识点 → 相邻知识点 → 回炉 → 提前复习（到期复习始终优先，review 模式不扩展）。

    参数:
        bcon: 题库连接
        lcon: 学习记录连接
        concepts: 知识点 ID 或前缀列表
        university: 学校
        program: 专攻关键词
        mode: adaptive / ascending / review
        allow_seen: 是否允许把出过的题当新题再出
        expand: 新题出完时是否扩展到相邻知识点
        at: 本地时间（默认现在）
        rng: 随机数发生器（测试用）
    返回:
        dict；found 为 True 时另含 _detail、_focus_keys、_note 供调用方更新面板（以下划线开头，不返回给 Claude）
    """
    if mode not in ("adaptive", "ascending", "review"):
        return {"found": False, "reason": "mode 只能是 adaptive、ascending、review"}
    blocked = learner.open_issue_problems(lcon)

    def keep(rows):
        return [r for r in rows if r["problem_id"] not in blocked]

    prim = keep(bank.candidates(bcon, concepts, university, program, ("primary",)))
    sec = keep(bank.candidates(bcon, concepts, university, program, ("secondary",)))
    near = bank.neighbors(bcon, concepts) if expand and mode != "review" else []
    neigh = keep(bank.candidates(bcon, near, university, program, ("primary",))) if near else []
    progs = bank.matched_programs(bcon, program, university) if program else []
    if not (prim or sec or neigh):
        return {"found": False, "reason": "题库中没有符合条件的小问", "matched_programs": progs,
                "hint": "换个知识点或前缀，或去掉学校/专攻限制" + ("（专攻关键词没有匹配到任何专攻）" if program and not progs else "")}
    state = mastery.replay(learner.all_attempts(lcon), until=learner.stamp(at))
    served = learner.served_problems(lcon)
    pick = selector.choose([(1, prim), (2, sec), (3, neigh)], served, learner.problem_history(lcon),
                           learner.card_retrievability(lcon, at), state,
                           learner.due_cards(lcon, at, concept_prefixes=concepts), mode, allow_seen, concepts, at, rng)
    if not pick:
        return {"found": False, "matched_programs": progs,
                "reason": "相关的题都做过了，也没有到期、待回炉或可以提前复习的题（距上次作答不到 20 小时的题暂不重出）",
                "hint": "可以设 allow_seen=true 再做一遍，换别的知识点，或放宽学校/专攻限制"}
    detail = bank.problem_detail(bcon, pick["problem_id"])
    focus_keys = [r["subq_key"] for r in pick["focus"]]
    learner.log_served(lcon, pick["problem_id"], focus_keys, concepts,
                       {"university": university, "program": program, "mode": mode},
                       pick["predicted"], pick["pool"], pick["level"], "next_question", at)
    out = present(detail, focus_keys)
    expansion = None
    if pick["level"] >= 2:
        names = insights.concept_info(bcon)
        to = sorted({r["concept_id"] for r in pick["focus"]})
        expansion = {"level": pick["level"], "from": concepts,
                     "to": [{"concept_id": c, "name": names.get(c, {}).get("name", "")} for c in to],
                     "note": "所选知识点的新题已做完，" + ("这题把它作为次要考点" if pick["level"] == 2 else "这题考相邻知识点")}
    remaining = len({r["problem_id"] for r in prim + sec} - served - {pick["problem_id"]})
    out.update(found=True, pool=pick["pool"], level=pick["level"], expansion=expansion, predicted=pick["predicted"],
               remaining_new_problems=remaining, matched_programs=progs, _detail=detail, _focus_keys=focus_keys,
               _note="" if pick["pool"].startswith("新题") and pick["level"] == 1 else pick["pool"])
    return out


def serve_problem(bcon, lcon, problem_id, subq_labels=None, at=None):
    """
    把指定的一道题出给学生（记录出题）。

    参数:
        bcon: 题库连接
        lcon: 学习记录连接
        problem_id: 大题 ID
        subq_labels: 本次要做的小问编号（任意写法）；留空表示整道题
        at: 本地时间（默认现在）
    返回:
        dict，格式同 next_question
    """
    detail = bank.problem_detail(bcon, problem_id)
    if not detail:
        return {"found": False, "reason": f"找不到 {problem_id}"}
    keys = [s["key"] for s in detail["subquestions"]]
    if subq_labels:
        keys, bad = bank.resolve_subqs(detail, subq_labels)
        if bad:
            return {"found": False, "reason": f"这道题没有小问 {bad}",
                    "all_subquestion_labels": [s["label"] for s in detail["subquestions"]]}
    state = mastery.replay(learner.all_attempts(lcon), until=learner.stamp(at))
    rows = bank.subq_rows(detail, keys)
    predicted = round(sum(mastery.predicted_success(state, r["primary"], r["difficulty"], at) for r in rows)
                      / max(1, len(rows)), 2)
    learner.log_served(lcon, problem_id, keys, [], {"via": "serve_problem"}, predicted, "指定题目", 0, "serve_problem", at)
    out = present(detail, keys)
    out.update(found=True, pool="指定题目", level=0, expansion=None, predicted=predicted,
               _detail=detail, _focus_keys=keys, _note="指定题目")
    return out


def record_attempt(bcon, lcon, problem_id, results=None, result="", subq_labels=None, hints=0, solution_viewed=False,
                   minutes=None, self_report="", error_type="", note="", verified=False, skip_reason="", at=None):
    """
    按小问记录一次作答反馈，并返回掌握度变化与下次复习时间。参数含义见 server.record_attempt。

    返回:
        dict
    """
    detail = bank.problem_detail(bcon, problem_id)
    if not detail:
        return {"recorded": False, "error": f"找不到 {problem_id}"}
    valid = [s["label"] for s in detail["subquestions"]]
    mapping = {}
    if results:
        for lab, res in results.items():
            keys, bad = bank.resolve_subqs(detail, [lab])
            if bad:
                return {"recorded": False, "error": f"这道题没有小问 {lab}", "all_subquestion_labels": valid}
            mapping.update({k: res for k in keys})
    else:
        if not result:
            return {"recorded": False, "error": "需要 result（或 results）"}
        if subq_labels:
            keys, bad = bank.resolve_subqs(detail, subq_labels)
            if bad:
                return {"recorded": False, "error": f"这道题没有小问 {bad}", "all_subquestion_labels": valid}
        else:
            keys = learner.default_focus(lcon, problem_id, at)
            if not keys:
                return {"recorded": False, "all_subquestion_labels": valid,
                        "error": "请用 subq_labels 或 results 写明小问（这道题不是最近一次出的题，或已超过 6 小时）"}
        mapping = {k: result for k in keys}
    bad = sorted({r for r in mapping.values() if r not in learner.RESULTS})
    if bad:
        return {"recorded": False, "error": f"结果只能是 {learner.RESULTS}，收到 {bad}"}
    if error_type and error_type not in learner.ERROR_TYPES:
        note, error_type = f"{error_type}；{note}".strip("；"), "其他"
    if skip_reason and skip_reason not in learner.SKIP_REASONS:
        note, skip_reason = f"{skip_reason}；{note}".strip("；"), "其他"
    served = learner.last_served(lcon, problem_id)
    current = learner.last_served(lcon)
    total, source = minutes, ("自报" if minutes is not None else "")
    if total is None and served and current and current["id"] == served["id"]:
        since = max(served["ts"], learner.last_activity(lcon, served["id"]) or "")
        elapsed = ((at or datetime.now()) - learner.parse(since)).total_seconds() / 60
        if 0.5 <= elapsed <= max(60, 3 * (detail["est_minutes"] or 30)):
            total, source = round(elapsed, 1), "估计"
    rows = bank.subq_rows(detail, list(mapping), SECONDARY_WEIGHT)
    for r in rows:
        res = mapping[r["key"]]
        r.update(result=res, score=mastery.attempt_score(res, hints, solution_viewed), self_report=self_report,
                 hints=hints, solution_viewed=solution_viewed,
                 minutes=round(total / len(rows), 1) if total is not None else None, minutes_source=source,
                 error_type=error_type if res in ("不会", "部分会") else "", note=note, verified=verified,
                 skip_reason=skip_reason if res == "跳过" else "")
    before = mastery.replay(learner.all_attempts(lcon), until=learner.stamp(at))
    recs = learner.record_attempts(lcon, problem_id, rows, served["id"] if served else None, at)
    after = mastery.replay(learner.all_attempts(lcon), until=learner.stamp(at))
    names = insights.concept_info(bcon)
    weights = {}
    for r in rows:
        if r["result"] != "跳过":
            for c, w in r["weights"].items():
                weights[c] = max(w, weights.get(c, 0))
    changes = [{"concept_id": c, "name": names.get(c, {}).get("name", ""), "weight": w,
                "from": mastery.summary(before, c, at)["mastery"], "to": mastery.summary(after, c, at)["mastery"],
                "level": mastery.summary(after, c, at)["level"]} for c, w in sorted(weights.items(), key=lambda x: -x[1])]
    by_key = {r["key"]: r for r in rows}
    return {"recorded": True, "minutes": total, "minutes_source": source,
            "subquestions": [dict({"label": x["label"], "result": by_key[x["key"]]["result"],
                                   "score": by_key[x["key"]]["score"], "next_review": x["next_review"]},
                                  **({"updated": True} if x["amended"] else {})) for x in recs],
            "mastery_changes": changes}
