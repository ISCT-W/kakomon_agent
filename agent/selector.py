"""
选题策略：在分层的候选小问中挑出下一道大题。

候选分三层：1 所选知识点是主知识点的小问；2 所选知识点是次知识点的小问；3 相邻知识点（主知识点）的小问。
- adaptive（默认）：到期复习 → 第 1、2、3 层的新题（预测做对概率最接近 70%，刚接触时从最简单的开始）
  → 回炉（之前不会/部分会、且距上次作答超过 REDO_GAP_HOURS 小时）→ 提前复习（最可能忘的先出）。
- ascending：第 1、2、3 层的新题，从最简单的开始；之后同上。
- review：只出做过的题：到期复习 → 回炉 → 提前复习，不扩展到相邻知识点。
难度与预测概率相同时随机挑选，避免总出同一所学校。
"""
import random
from collections import defaultdict
from datetime import datetime, timedelta

import mastery

REDO_GAP_HOURS = 20
LEVEL_NAMES = {1: "所选知识点", 2: "所选知识点（作为次要考点）", 3: "相邻知识点"}
TS_FMT = "%Y-%m-%d %H:%M:%S"


def group(rows):
    """
    按大题分组候选小问（同一小问多行时只留一行）。

    参数:
        rows: 候选小问行
    返回:
        dict，题目 ID -> list[行]
    """
    out = defaultdict(dict)
    for r in rows:
        out[r["problem_id"]].setdefault(r["subq_key"], r)
    return {pid: sorted(v.values(), key=lambda r: r["subq_seq"]) for pid, v in out.items()}


def choose(levels, served, history, retr, state, due, mode="adaptive", allow_seen=False, prefixes=(), at=None, rng=None):
    """
    挑选下一道大题。

    参数:
        levels: list[(层级, 候选小问行)]，层级 1/2/3
        served: 已出过的大题集合
        history: learner.problem_history 的结果
        retr: learner.card_retrievability 的结果
        state: mastery.replay 的结果
        due: learner.due_cards 返回的到期复习题（已按所选知识点过滤）
        mode: adaptive / ascending / review
        allow_seen: 是否允许把出过的题当新题再出
        prefixes: 本次所选知识点 ID 或前缀
        at: 本地时间 datetime（默认现在）
        rng: 随机数发生器（测试用）
    返回:
        dict 或 None，含 problem_id、focus（命中的小问行）、key_difficulty、predicted、pool、level
    """
    rng = rng or random.Random()
    now = at or datetime.now()
    groups = {lv: group(rows) for lv, rows in levels}
    own = {}
    for lv in (1, 2):
        for pid, rows in groups.get(lv, {}).items():
            own.setdefault(pid, rows)

    def stats(rows):
        key = min(r["difficulty"] or 3 for r in rows)
        pred = sum(mastery.predicted_success(state, [r["concept_id"]], r["difficulty"], now) for r in rows) / len(rows)
        return key, pred

    def result(pid, rows, pool, level):
        key, pred = stats(rows)
        return {"problem_id": pid, "focus": rows, "key_difficulty": key, "predicted": round(pred, 2),
                "pool": pool, "level": level}

    def old_enough(pid):
        ts = history.get(pid, {}).get("last_ts")
        return not ts or now - datetime.strptime(ts, TS_FMT) >= timedelta(hours=REDO_GAP_HOURS)

    def pick_due():
        for d in due:
            rows = [r for r in own.get(d["problem_id"], []) if r["subq_key"] in d["keys"]] or own.get(d["problem_id"])
            if rows:
                return result(d["problem_id"], rows, "到期复习", 0)
        return None

    def pick_redo():
        bad = {pid: [r for r in rows if r["subq_key"] in history.get(pid, {}).get("bad_keys", set())]
               for pid, rows in own.items()}
        ids = [pid for pid, rows in bad.items() if rows and old_enough(pid)]
        if not ids:
            return None
        pid = min(ids, key=lambda p: (stats(bad[p])[0], rng.random()))
        return result(pid, bad[pid], "回炉（之前不会或部分会的题）", 0)

    def pick_early():
        ids = [pid for pid in own if pid in served and old_enough(pid)]
        if not ids:
            return None
        pid = min(ids, key=lambda p: (retr.get(p, 1.0), rng.random()))
        return result(pid, own[pid], "提前复习（相关的新题都做完了）", 0)

    def pick_new():
        for lv in (1, 2, 3):
            new = {pid: rows for pid, rows in groups.get(lv, {}).items() if allow_seen or pid not in served}
            if lv > 1:
                new = {pid: rows for pid, rows in new.items() if not any(pid in groups.get(x, {}) for x in range(1, lv))
                       or allow_seen}
            if not new:
                continue
            fresh = mode == "ascending" or mastery.experience(state, prefixes) < 1
            if fresh:
                pid = min(new, key=lambda p: (stats(new[p])[0], rng.random()))
                pool = "新题（从最简单的开始）"
            else:
                pid = min(new, key=lambda p: (abs(stats(new[p])[1] - mastery.TARGET_P), stats(new[p])[0], rng.random()))
                pool = "新题（预测做对概率约 70%）"
            return result(pid, new[pid], pool if lv == 1 else f"{pool}，{LEVEL_NAMES[lv]}", lv)
        return None

    if mode == "review":
        return pick_due() or pick_redo() or pick_early()
    if mode == "adaptive":
        hit = pick_due()
        if hit:
            return hit
    return pick_new() or pick_redo() or pick_early()
