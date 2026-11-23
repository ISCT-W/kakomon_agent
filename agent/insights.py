"""
学习分析：掌握情况（当前与历史最高）、题型强弱、回避倾向、学习习惯、预测校准、学习数据摘要（画像的证据部分）、
学习计划与周期报告。

组合题库（bank）、学习记录（learner）、掌握度（mastery）与复习排期（review 卡片）。
所有函数都接受 at（本地时间），便于按任意时刻重算。统计结论都有最小样本量，样本不够不下结论。
"""
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import bank
import learner
import mastery

MIN_SAMPLES = 5
SESSION_GAP_MIN = 45
ABANDON_HOURS = 6
TYPE_NAMES = {"calculation": "计算", "proof": "证明", "explanation": "说明论述", "programming": "编写程序",
              "code_reading": "读程序", "design": "设计", "fill_in": "填空", "choice": "选择", "other": "其他"}


def concept_info(bank_con):
    """
    知识点 ID -> 名称、科目、单元。

    参数:
        bank_con: 题库连接
    返回:
        dict
    """
    return {r["concept_id"]: {"name": r["name"], "subject": r["subject"], "unit": r["unit"]}
            for r in bank_con.execute("SELECT concept_id, name, subject, unit FROM concepts")}


def state_at(learn_con, at=None):
    """
    重放到某一时刻为止的作答记录，得到掌握度状态。

    参数:
        learn_con: 学习记录连接
        at: 本地时间（默认现在）
    返回:
        dict，mastery.replay 的结果
    """
    return mastery.replay(learner.all_attempts(learn_con, until=learner.stamp(at)))


def answered(attempts):
    """
    去掉“跳过”的作答。

    参数:
        attempts: 作答记录
    返回:
        list
    """
    return [a for a in attempts if a["result"] != "跳过"]


def progress(bank_con, learn_con, prefix="", at=None):
    """
    各知识点的掌握情况（当前掌握度低的在前）。

    参数:
        bank_con: 题库连接
        learn_con: 学习记录连接
        prefix: 只看某个科目或单元前缀
        at: 本地时间（默认现在）
    返回:
        dict，含 total_attempts、verified_ratio、self_report_factor、due_reviews、concepts、by_type
    """
    info = concept_info(bank_con)
    attempts = learner.all_attempts(learn_con, until=learner.stamp(at))
    done = answered(attempts)
    state = mastery.replay(attempts)
    due = learner.due_cards(learn_con, at)
    due_by_c = Counter(c for d in due for c in d["concept_ids"])
    agg = defaultdict(lambda: {"scores": [], "errors": Counter(), "last": ""})
    for a in done:
        for c, w in json.loads(a["concept_weights"]).items():
            if w >= 1.0:
                agg[c]["scores"].append(a["score"])
                agg[c]["last"] = a["result"]
                if a["error_type"]:
                    agg[c]["errors"][a["error_type"]] += 1
    rows = []
    for c in state:
        if prefix and not c.startswith(prefix):
            continue
        g = agg[c]
        rows.append(dict(concept_id=c, **info.get(c, {"name": "", "subject": "", "unit": ""}),
                         **mastery.summary(state, c, at),
                         avg_score=round(sum(g["scores"]) / len(g["scores"]), 2) if g["scores"] else None,
                         last_result=g["last"], errors=dict(g["errors"]), due_reviews=due_by_c.get(c, 0)))
    rows.sort(key=lambda r: (r["mastery"], -r["n"]))
    verified = sum(1 for a in done if a["verified"])
    return {"total_attempts": len(done), "verified_ratio": round(verified / max(1, len(done)), 2),
            "self_report_factor": mastery.self_report_factor(attempts), "due_reviews": len(due),
            "concepts": rows, "by_type": type_stats(bank_con, attempts)}


def speed_ratio(a, meta):
    """
    一条作答的用时比（实际分钟 / 该小问的预计分钟，预计分钟按整题预计用时平均分到各小问）。

    参数:
        a: 作答记录
        meta: bank.problem_meta 的结果
    返回:
        float 或 None
    """
    m = meta.get(a["problem_id"])
    if not a["minutes"] or not m or not m["est_minutes"] or not m["n_subq"]:
        return None
    return a["minutes"] / (m["est_minutes"] / m["n_subq"])


def type_stats(bank_con, attempts):
    """
    按题型统计：作答数、平均得分、跳过数、看提示的比例、平均用时比。

    参数:
        bank_con: 题库连接
        attempts: 作答记录
    返回:
        list[dict]，按作答数从多到少
    """
    meta = bank.problem_meta(bank_con)
    g = defaultdict(lambda: {"n": 0, "score": 0.0, "skips": 0, "hinted": 0, "speed": []})
    for a in attempts:
        t = a["question_type"] or "other"
        if a["result"] == "跳过":
            g[t]["skips"] += 1
            continue
        g[t]["n"] += 1
        g[t]["score"] += a["score"] or 0
        g[t]["hinted"] += 1 if a["hints"] else 0
        r = speed_ratio(a, meta)
        if r:
            g[t]["speed"].append(r)
    out = []
    for t, v in g.items():
        out.append({"question_type": t, "name": TYPE_NAMES.get(t, t), "attempts": v["n"], "skips": v["skips"],
                    "avg_score": round(v["score"] / v["n"], 2) if v["n"] else None,
                    "hint_ratio": round(v["hinted"] / v["n"], 2) if v["n"] else None,
                    "speed_ratio": round(sum(v["speed"]) / len(v["speed"]), 2) if v["speed"] else None})
    return sorted(out, key=lambda x: -(x["attempts"] + x["skips"]))


def sessions(learn_con, at=None):
    """
    由出题与作答时间推算学习会话（相邻两次活动间隔超过 SESSION_GAP_MIN 分钟算新会话）。

    参数:
        learn_con: 学习记录连接
        at: 本地时间（默认现在）
    返回:
        list[dict]，含 start、end、minutes、attempts
    """
    until = learner.stamp(at)
    events = sorted([(r["ts"], 0) for r in learner.all_served(learn_con, until=until)] +
                    [(r["ts"], 1) for r in learner.all_attempts(learn_con, until=until)])
    out = []
    for ts, is_attempt in events:
        t = learner.parse(ts)
        if out and (t - out[-1]["_end"]) <= timedelta(minutes=SESSION_GAP_MIN):
            out[-1]["_end"] = t
            out[-1]["attempts"] += is_attempt
        else:
            out.append({"_start": t, "_end": t, "attempts": is_attempt})
    return [{"start": s["_start"].strftime(learner.TS_FMT), "end": s["_end"].strftime(learner.TS_FMT),
             "minutes": round((s["_end"] - s["_start"]).total_seconds() / 60), "attempts": s["attempts"]} for s in out]


def habits(learn_con, at=None, days=28):
    """
    学习习惯：最近 days 天的学习天数、会话数与平均时长、常学时段、连续学习天数、到期复习积压。

    参数:
        learn_con: 学习记录连接
        at: 本地时间（默认现在）
        days: 统计窗口（天）
    返回:
        dict
    """
    now = at or datetime.now()
    ss = [s for s in sessions(learn_con, at) if learner.parse(s["start"]) >= now - timedelta(days=days)]
    day_set = {s["start"][:10] for s in ss}
    hours = Counter(int(s["start"][11:13]) // 3 * 3 for s in ss)
    streak, d = 0, now.date()
    while d.strftime("%Y-%m-%d") in day_set:
        streak += 1
        d -= timedelta(days=1)
    due = learner.due_cards(learn_con, at)
    return {"window_days": days, "active_days": len(day_set), "sessions": len(ss),
            "avg_session_minutes": round(sum(s["minutes"] for s in ss) / len(ss)) if ss else None,
            "usual_hours": [f"{h:02d}-{h + 3:02d} 点" for h, _ in hours.most_common(2)],
            "streak_days": streak, "due_backlog": len(due),
            "overdue_avg_days": round(sum(x["overdue_days"] for x in due) / len(due), 1) if due else 0}


def avoidance(bank_con, learn_con, at=None):
    """
    回避倾向：跳过率（按题型）与“出了没做”的题数（出题后 ABANDON_HOURS 小时内没有任何记录）。

    参数:
        bank_con: 题库连接
        learn_con: 学习记录连接
        at: 本地时间（默认现在）
    返回:
        dict，含 skip_by_type、skip_reasons、abandoned、served
    """
    now = at or datetime.now()
    attempts = learner.all_attempts(learn_con, until=learner.stamp(at))
    served = learner.all_served(learn_con, until=learner.stamp(at))
    by_type = defaultdict(lambda: [0, 0])
    for a in attempts:
        t = TYPE_NAMES.get(a["question_type"], a["question_type"] or "其他")
        by_type[t][1] += 1
        by_type[t][0] += 1 if a["result"] == "跳过" else 0
    touched = defaultdict(list)
    for a in attempts:
        touched[a["problem_id"]].append(learner.parse(a["ts"]))
    abandoned = 0
    for s in served:
        t0 = learner.parse(s["ts"])
        if now - t0 < timedelta(hours=ABANDON_HOURS):
            continue
        if not any(t0 <= t <= t0 + timedelta(hours=ABANDON_HOURS) for t in touched.get(s["problem_id"], [])):
            abandoned += 1
    return {"skip_by_type": {t: {"skips": v[0], "total": v[1]} for t, v in by_type.items() if v[0]},
            "skip_reasons": dict(Counter(a["skip_reason"] for a in attempts if a["result"] == "跳过" and a["skip_reason"])),
            "abandoned": abandoned, "served": len(served)}


def calibration(learn_con, at=None):
    """
    预测校准：出题时的预测做对概率与这些题实际得分的对比（只算新题）。

    参数:
        learn_con: 学习记录连接
        at: 本地时间（默认现在）
    返回:
        dict，含 n、predicted、actual、gap；样本不足时 n 小于 MIN_SAMPLES
    """
    until = learner.stamp(at)
    scores = defaultdict(list)
    for a in learner.all_attempts(learn_con, until=until):
        if a["served_id"] and a["result"] != "跳过":
            scores[a["served_id"]].append(a["score"] or 0)
    pairs = [(s["predicted"], sum(scores[s["id"]]) / len(scores[s["id"]]))
             for s in learner.all_served(learn_con, until=until)
             if s["predicted"] is not None and s["level"] and scores.get(s["id"])]
    if not pairs:
        return {"n": 0}
    p = sum(x for x, _ in pairs) / len(pairs)
    y = sum(x for _, x in pairs) / len(pairs)
    return {"n": len(pairs), "predicted": round(p, 2), "actual": round(y, 2), "gap": round(y - p, 2)}


def facts(bank_con, learn_con, at=None):
    """
    学习数据摘要（画像的证据部分）：由代码算出、样本足够才列出的事实，每条带编号与依据。

    参数:
        bank_con: 题库连接
        learn_con: 学习记录连接
        at: 本地时间（默认现在）
    返回:
        list[dict]，含 id、category、text
    """
    out = []

    def add(category, text):
        out.append({"id": f"F{len(out) + 1}", "category": category, "text": text})

    prog = progress(bank_con, learn_con, at=at)
    attempts = learner.all_attempts(learn_con, until=learner.stamp(at))
    done = answered(attempts)
    if not done:
        return out
    add("总体", f"共作答 {len(done)} 个小问，经批改确认的占 {prog['verified_ratio']:.0%}")
    tried = [c for c in prog["concepts"] if c["n"] >= 2]
    weak = [c for c in tried if c["level"] == "薄弱"][:3]
    if weak:
        add("薄弱知识点", "；".join(f"{c['name']}（当前掌握度 {c['mastery']}，加权作答 {c['n']}）" for c in weak))
    strong = sorted([c for c in tried if c["level"] == "掌握"], key=lambda c: -c["mastery"])[:3]
    if strong:
        add("强项", "；".join(f"{c['name']}（{c['mastery']}）" for c in strong))
    faded = [c for c in tried if c["mastery_peak"] - c["mastery"] >= 0.1]
    if faded:
        add("遗忘", "；".join(f"{c['name']} 从最高 {c['mastery_peak']} 回落到 {c['mastery']}（{c['days_since']} 天没练）"
                            for c in faded[:3]))
    types = [t for t in prog["by_type"] if t["attempts"] >= MIN_SAMPLES]
    if len(types) >= 2:
        hi, lo = max(types, key=lambda t: t["avg_score"]), min(types, key=lambda t: t["avg_score"])
        if hi["avg_score"] - lo["avg_score"] >= 0.2:
            add("题型", f"{lo['name']}题平均得分 {lo['avg_score']}（{lo['attempts']} 次），明显低于{hi['name']}题 "
                       f"{hi['avg_score']}（{hi['attempts']} 次）")
    errors = Counter(a["error_type"] for a in done if a["error_type"])
    if sum(errors.values()) >= MIN_SAMPLES:
        top, n = errors.most_common(1)[0]
        add("错因", f"最常见的错因是“{top}”，占 {n}/{sum(errors.values())} 次")
    hinted = [a for a in done if a["hints"]]
    if len(done) >= MIN_SAMPLES and hinted:
        add("提示依赖", f"{len(hinted)}/{len(done)} 个小问看了提示才完成")
    speeds = [t for t in prog["by_type"] if t["speed_ratio"] and t["attempts"] >= MIN_SAMPLES]
    slow = [t for t in speeds if t["speed_ratio"] >= 1.3]
    if slow:
        add("速度", "；".join(f"{t['name']}题用时约为预计的 {t['speed_ratio']} 倍" for t in slow))
    av = avoidance(bank_con, learn_con, at)
    skipped = {t: v for t, v in av["skip_by_type"].items() if v["total"] >= MIN_SAMPLES and v["skips"] / v["total"] >= 0.3}
    if skipped:
        add("回避", "；".join(f"{t}题跳过 {v['skips']}/{v['total']}" for t, v in skipped.items()))
    if av["served"] >= MIN_SAMPLES and av["abandoned"] / av["served"] >= 0.2:
        add("回避", f"出了题却没有任何反馈的有 {av['abandoned']}/{av['served']} 道")
    if prog["self_report_factor"] < mastery.SELF_REPORT_DEFAULT:
        add("自评", f"自报“会”的题批改后常有错误，自报折扣已调到 {prog['self_report_factor']}")
    hb = habits(learn_con, at)
    if hb["sessions"] >= 3:
        add("习惯", f"最近 {hb['window_days']} 天学习 {hb['active_days']} 天、{hb['sessions']} 次，平均每次 "
                   f"{hb['avg_session_minutes']} 分钟，常在 {'、'.join(hb['usual_hours'])}；连续学习 {hb['streak_days']} 天")
    if hb["due_backlog"] >= 10:
        add("复习积压", f"到期未复习 {hb['due_backlog']} 道，平均逾期 {hb['overdue_avg_days']} 天")
    cal = calibration(learn_con, at)
    if cal["n"] >= MIN_SAMPLES and abs(cal["gap"]) >= 0.15:
        add("难度校准", f"新题预测做对率平均 {cal['predicted']}，实际得分 {cal['actual']}（{cal['n']} 题），"
                       f"{'题目对你偏简单' if cal['gap'] > 0 else '题目对你偏难'}")
    return out


def profile_payload(bank_con, learn_con, at=None):
    """
    get_profile 的返回内容：学习数据摘要、观察记录、画像解读，以及画像是否需要更新。

    参数:
        bank_con: 题库连接
        learn_con: 学习记录连接
        at: 本地时间（默认现在）
    返回:
        dict
    """
    n = len(answered(learner.all_attempts(learn_con, until=learner.stamp(at))))
    last = learner.last_profile_save(learn_con)
    stale, reason = False, ""
    if last:
        days = ((at or datetime.now()) - learner.parse(last["ts"])).days
        if n - (last["attempts_at"] or 0) >= 15:
            stale, reason = True, f"上次更新后又作答了 {n - (last['attempts_at'] or 0)} 个小问"
        elif days >= 7 and n > (last["attempts_at"] or 0):
            stale, reason = True, f"上次更新是 {days} 天前"
    elif n >= 8:
        stale, reason = True, "还没有画像，作答已足够写第一版"
    return {"facts": facts(bank_con, learn_con, at), "observations": learner.list_observations(learn_con, 20),
            "profile": learner.get_profile(), "stale": stale, "stale_reason": reason, "attempts": n,
            "last_update": last["ts"] if last else ""}


def concept_frequency(bank_con, universities=None, programs=None, subjects=None):
    """
    目标学校/专攻过去问中，考到各知识点（作为主知识点）的大题数（近年加权：最早 0.5、最新 1.0）。
    逐级放宽：学校＋专攻 → 只看学校 → 全部学校，并返回实际用了哪一级。

    参数:
        bank_con: 题库连接
        universities: 学校列表（可用简称），为空表示全部
        programs: 专攻关键词列表，为空表示全部
        subjects: 只看这些科目（科目 ID、中文名或科目组“数学”），为空表示全部
    返回:
        (dict 知识点 -> 加权频次, str 匹配级别：学校与专攻 / 学校 / 全部)
    """
    rows = bank_con.execute("""
        SELECT DISTINCT sc.concept_id, p.problem_id, p.admission_year AS year, d.university,
               d.program || ' ' || COALESCE(d.program_folder, '') AS program
        FROM subq_concepts sc JOIN subquestions sq ON sq.subq_id = sc.subq_id
        JOIN problems p ON p.problem_id = sq.problem_id JOIN documents d ON d.doc_id = p.doc_id
        WHERE sc.role = 'primary' AND (p.duplicate_of IS NULL OR p.duplicate_of = '')""").fetchall()
    sids = bank.resolve_subjects(bank_con, subjects or [])
    rows = [r for r in rows if bank.in_subjects(r["concept_id"], sids)]
    unis = bank.resolve_universities(universities or [])
    parts = [x for p in (programs or []) for x in bank.resolve_program(p)]

    def by_uni(r):
        return not unis or any(u in r["university"] for u in unis)

    level, sel = "全部", rows
    both = [r for r in rows if by_uni(r) and bank.program_match(r["program"], parts)] if parts else []
    uni_only = [r for r in rows if by_uni(r)] if unis else []
    if both:
        level, sel = "学校与专攻", both
    elif uni_only:
        level, sel = "学校", uni_only
    years = [r["year"] for r in sel if r["year"]] or [0]
    lo, hi = min(years), max(years)
    freq = Counter()
    for r in sel:
        freq[r["concept_id"]] += 0.5 + 0.5 * ((r["year"] or lo) - lo) / max(1, hi - lo)
    return dict(freq), level


def study_plan(bank_con, learn_con, universities=None, programs=None, top_n=8, at=None, subjects=None):
    """
    学习计划：按“目标学校出题频率 ×（1 - 当前掌握度）”排序，冲刺期重点知识点加权，有到期复习的加分。

    参数:
        bank_con: 题库连接
        learn_con: 学习记录连接
        universities: 目标学校（默认取当前目标）
        programs: 目标专攻关键词（默认：没指定学校时取当前目标的专攻，指定了学校时不限专攻）
        top_n: 返回前几个知识点
        at: 本地时间（默认现在）
        subjects: 只看这些科目（科目 ID、中文名或科目组“数学”）
    返回:
        dict，含 target、matched_target、notice、days_to_exam、plan、due_reviews
    """
    goal = learner.active_goal(learn_con)
    if universities is None and goal:
        universities = goal["universities"]
        programs = goal["programs"] if programs is None else programs
    freq, level = concept_frequency(bank_con, universities, programs, subjects)
    wanted = "学校与专攻" if programs else ("学校" if universities else "全部")
    matched = level == wanted
    top = max(freq.values()) if freq else 1.0
    state = state_at(learn_con, at)
    due = learner.due_cards(learn_con, at)
    due_by_c = Counter(c for d in due for c in d["concept_ids"])
    focus = [(p["name"], f) for p in learner.list_periods(learn_con, at) if p["status"] == "进行中" for f in p["focus"]]
    info = concept_info(bank_con)
    rows = []
    for c, f in freq.items():
        m = mastery.summary(state, c, at)
        boost = [name for name, pre in focus if c.startswith(pre)]
        priority = (f / top) * (1 - m["mastery"]) * (1.5 if boost else 1.0) + 0.3 * due_by_c.get(c, 0)
        reasons = [f"目标卷中出现 {f:.1f} 次（近年加权）", f"当前掌握度 {m['mastery']}（{m['level']}）"]
        if m["mastery_peak"] - m["mastery"] >= 0.1:
            reasons.append(f"比最高时的 {m['mastery_peak']} 有所遗忘")
        if due_by_c.get(c):
            reasons.append(f"{due_by_c[c]} 道题到期待复习")
        if boost:
            reasons.append(f"属于进行中的期间「{boost[0]}」的重点")
        rows.append(dict(concept_id=c, **info.get(c, {}), priority=round(priority, 3), level=m["level"],
                         mastery=m["mastery"], reasons=reasons))
    rows.sort(key=lambda r: -r["priority"])
    day = learner.exam_day(goal["exam_date"]) if goal else None
    days = (day - (at or datetime.now()).date()).days if day else None
    notice = ""
    if not matched:
        notice = ("目标专攻在题库里还没有题，下面按目标学校全部专攻的出题频率排序" if level == "学校" else
                  "目标学校在题库里还没有题，下面按全部学校的出题频率排序") + "，请转告学生"
    return {"target": {"universities": universities or [], "programs": programs or []}, "matched_target": matched,
            "frequency_scope": level, "notice": notice, "days_to_exam": days, "plan": rows[:top_n],
            "due_reviews": len(due)}


def report(bank_con, learn_con, days=7, at=None):
    """
    周期报告：期间内作答量与结果、各科与各题型表现、掌握度变化、错因分布、学习习惯、到期复习、目标倒计时。

    参数:
        bank_con: 题库连接
        learn_con: 学习记录连接
        days: 统计最近几天
        at: 报告时刻（本地时间，默认现在）
    返回:
        dict
    """
    end = at or datetime.now()
    start = end - timedelta(days=days)
    info = concept_info(bank_con)
    period_all = learner.all_attempts(learn_con, since=learner.stamp(start), until=learner.stamp(end))
    period = answered(period_all)
    before, after = state_at(learn_con, start), state_at(learn_con, end)
    touched = {c for a in period for c in json.loads(a["concept_weights"])}
    changes = []
    for c in touched:
        m0, m1 = mastery.summary(before, c, start), mastery.summary(after, c, end)
        changes.append({"concept_id": c, "name": info.get(c, {}).get("name", ""), "from": m0["mastery"],
                        "to": m1["mastery"], "delta": round(m1["mastery"] - m0["mastery"], 2), "level": m1["level"]})
    changes.sort(key=lambda x: -x["delta"])
    by_subject = defaultdict(lambda: {"attempts": 0, "score_sum": 0.0})
    for a in period:
        prim = [c for c, w in json.loads(a["concept_weights"]).items() if w >= 1.0]
        for s in {info.get(c, {}).get("subject", "其他") for c in prim}:
            by_subject[s]["attempts"] += 1
            by_subject[s]["score_sum"] += a["score"] or 0
    subjects = sorted(({"subject": s, "attempts": v["attempts"], "avg_score": round(v["score_sum"] / v["attempts"], 2)}
                       for s, v in by_subject.items()), key=lambda x: -x["attempts"])
    goal = learner.active_goal(learn_con)
    return {
        "range": [learner.stamp(start), learner.stamp(end)],
        "attempts": len(period), "results": dict(Counter(a["result"] for a in period_all)),
        "active_days": len({a["ts"][:10] for a in period}),
        "verified": sum(1 for a in period if a["verified"]),
        "hinted": sum(1 for a in period if a["hints"]),
        "subjects": subjects, "by_type": type_stats(bank_con, period_all), "mastery_changes": changes,
        "errors": dict(Counter(a["error_type"] for a in period if a["error_type"])),
        "habits": habits(learn_con, end, days),
        "due_reviews": len(learner.due_cards(learn_con, end)),
        "goal": goal,
        "days_to_exam": (learner.exam_day(goal["exam_date"]) - end.date()).days
        if goal and learner.exam_day(goal["exam_date"]) else None,
        "periods": learner.list_periods(learn_con, end),
    }
