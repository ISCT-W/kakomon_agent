"""
学习记录（存储层）：出题记录、作答（每个小问一条，含提示次数、用时、自报结果、跳过原因）、按小问的复习卡片、
目标与重要期间、观察记录、学习画像版本、题目报错。

数据存放在 data/learner.sqlite（与题库分开，可随时备份）；画像正文在 data/learner_profile.md。
所有写入函数都接受 at（本地时间 datetime），便于测试时模拟多天的学习过程。
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import review

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_DB = os.path.join(DATA_DIR, "learner.sqlite")
PROFILE_PATH = os.path.join(DATA_DIR, "learner_profile.md")

SCHEMA_VERSION = 2
RESULTS = ["会", "部分会", "不会", "跳过"]
ERROR_TYPES = ["没思路", "卡在中间步骤", "计算错误", "概念不清", "忘记公式", "读题有误", "时间不够", "其他"]
SKIP_REASONS = ["太难", "没时间", "不想做这类", "已经会了", "其他"]
TS_FMT = "%Y-%m-%d %H:%M:%S"
FOCUS_WINDOW_HOURS = 6

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS served (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, problem_id TEXT, focus TEXT, concepts TEXT, request TEXT,
    predicted REAL, pool TEXT, level INTEGER, via TEXT);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, served_id INTEGER, problem_id TEXT, subq_key TEXT, subq_label TEXT,
    concept_weights TEXT, difficulty INTEGER, question_type TEXT, result TEXT, self_report TEXT, score REAL,
    hints INTEGER, solution_viewed INTEGER, minutes REAL, minutes_source TEXT, error_type TEXT, note TEXT,
    verified INTEGER, skip_reason TEXT);
CREATE TABLE IF NOT EXISTS cards (
    problem_id TEXT, subq_key TEXT, card TEXT, due TEXT, last_result TEXT, last_ts TEXT, reps INTEGER,
    concept_ids TEXT, PRIMARY KEY (problem_id, subq_key));
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, problem_id TEXT, description TEXT, status TEXT DEFAULT 'open',
    resolution TEXT);
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, exam_date TEXT, universities TEXT, programs TEXT,
    note TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, name TEXT, start_date TEXT, end_date TEXT, focus TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, text TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS profile_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, markdown TEXT, attempts_at INTEGER);
CREATE INDEX IF NOT EXISTS idx_attempts_problem ON attempts(problem_id);
CREATE INDEX IF NOT EXISTS idx_served_problem ON served(problem_id);
"""


def stamp(at=None):
    """
    本地时间字符串。

    参数:
        at: 本地时间 datetime（默认现在）
    返回:
        str，形如 2026-11-06 15:04:05
    """
    return (at or datetime.now()).strftime(TS_FMT)


def to_utc(at=None):
    """
    把本地时间转成带时区的 UTC 时间（FSRS 使用）。

    参数:
        at: 本地时间 datetime（默认现在）
    返回:
        datetime
    """
    return (at or datetime.now()).astimezone(timezone.utc)


def parse(ts):
    """
    解析本地时间字符串。

    参数:
        ts: 时间字符串
    返回:
        datetime
    """
    return datetime.strptime(ts, TS_FMT)


def connect(path=None):
    """
    打开学习记录数据库并建表。旧版结构的空库会被重建；旧版结构且有数据时报错，避免误删记录。

    参数:
        path: 数据库路径，默认环境变量 KAKOMON_LEARNER_DB 或 data/learner.sqlite
    返回:
        sqlite3.Connection
    """
    con = sqlite3.connect(path or os.environ.get("KAKOMON_LEARNER_DB", DEFAULT_DB), check_same_thread=False)
    con.row_factory = sqlite3.Row
    if con.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        old = [t for t in ("served", "attempts", "cards", "profile_history", "issues") if t in tables]
        if any(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in old):
            raise RuntimeError("学习记录是旧版结构且已有数据，需要先迁移，不能直接使用")
        for t in old:
            con.execute(f"DROP TABLE {t}")
        con.executescript(SCHEMA_SQL)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.executescript(SCHEMA_SQL)
    con.commit()
    return con


def log_served(con, problem_id, focus_keys, concepts, request, predicted=None, pool="", level=0, via="", at=None):
    """
    记录一次出题。

    参数:
        con: 数据库连接
        problem_id: 大题 ID
        focus_keys: 本次重点小问的规范化键
        concepts: 本次请求的知识点
        request: 出题请求参数（dict）
        predicted: 预测做对概率
        pool: 来源（新题、到期复习……）
        level: 扩展层级（1 主知识点、2 次知识点、3 相邻知识点；复习类为 0）
        via: next_question / serve_problem
        at: 时间（默认现在）
    返回:
        int，出题记录 ID
    """
    cur = con.execute(
        "INSERT INTO served (ts, problem_id, focus, concepts, request, predicted, pool, level, via) VALUES (?,?,?,?,?,?,?,?,?)",
        (stamp(at), problem_id, json.dumps(focus_keys, ensure_ascii=False), json.dumps(concepts, ensure_ascii=False),
         json.dumps(request, ensure_ascii=False), predicted, pool, level, via))
    con.commit()
    return cur.lastrowid


def last_served(con, problem_id=None):
    """
    最近一次出题记录（可限定某道题）。

    参数:
        con: 数据库连接
        problem_id: 大题 ID（可选）
    返回:
        sqlite3.Row 或 None
    """
    if problem_id:
        return con.execute("SELECT * FROM served WHERE problem_id = ? ORDER BY id DESC LIMIT 1", (problem_id,)).fetchone()
    return con.execute("SELECT * FROM served ORDER BY id DESC LIMIT 1").fetchone()


def default_focus(con, problem_id, at=None):
    """
    记录作答时没写小问时的默认重点：只有最近一次出的就是这道题、且在 FOCUS_WINDOW_HOURS 小时内，才沿用它的重点小问。

    参数:
        con: 数据库连接
        problem_id: 大题 ID
        at: 时间（默认现在）
    返回:
        list[str]，规范化小问键；不能沿用时为空
    """
    r = last_served(con)
    if not r or r["problem_id"] != problem_id:
        return []
    if (at or datetime.now()) - parse(r["ts"]) > timedelta(hours=FOCUS_WINDOW_HOURS):
        return []
    return json.loads(r["focus"])


def record_attempts(con, problem_id, rows, served_id=None, at=None):
    """
    记录一次反馈涉及的各个小问（每个小问一条），并更新复习卡片（跳过不更新）。

    参数:
        con: 数据库连接
        problem_id: 大题 ID
        rows: list[dict]，每项含 key、label、weights、difficulty、question_type、primary、result、score、self_report、
              hints、solution_viewed、minutes、minutes_source、error_type、note、verified、skip_reason
        served_id: 对应的出题记录 ID
        at: 作答时间（本地时间，默认现在）
    返回:
        list[dict]，每个小问的记录 ID 与下次复习时间
    """
    out = []
    for r in rows:
        if r["result"] not in RESULTS:
            raise ValueError(f"result 必须是 {RESULTS} 之一")
        cur = con.execute(
            "INSERT INTO attempts (ts, served_id, problem_id, subq_key, subq_label, concept_weights, difficulty, "
            "question_type, result, self_report, score, hints, solution_viewed, minutes, minutes_source, error_type, "
            "note, verified, skip_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (stamp(at), served_id, problem_id, r["key"], r["label"], json.dumps(r["weights"], ensure_ascii=False),
             r["difficulty"], r["question_type"], r["result"], r["self_report"] or r["result"], r["score"],
             int(r["hints"] or 0), int(bool(r["solution_viewed"])), r["minutes"], r["minutes_source"],
             r["error_type"], r["note"], int(bool(r["verified"])), r["skip_reason"]))
        due = ""
        if r["result"] != "跳过":
            old = con.execute("SELECT card, reps FROM cards WHERE problem_id = ? AND subq_key = ?",
                              (problem_id, r["key"])).fetchone()
            card, due_dt = review.review(old["card"] if old else None, r["result"], to_utc(at),
                                         r["hints"], r["solution_viewed"])
            con.execute("INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?)",
                        (problem_id, r["key"], card, due_dt.isoformat(), r["result"], stamp(at),
                         (old["reps"] if old else 0) + 1, json.dumps(r["primary"], ensure_ascii=False)))
            due = due_dt.astimezone().strftime(TS_FMT)
        out.append({"id": cur.lastrowid, "key": r["key"], "label": r["label"], "next_review": due})
    con.commit()
    return out


def all_attempts(con, since=None, until=None):
    """
    作答记录（按时间顺序）。

    参数:
        con: 数据库连接
        since: 起始时间字符串（含），可选
        until: 截止时间字符串（含），可选
    返回:
        list[sqlite3.Row]
    """
    rows = con.execute("SELECT * FROM attempts ORDER BY id").fetchall()
    return [r for r in rows if (not since or r["ts"] >= since) and (not until or r["ts"] <= until)]


def all_served(con, since=None, until=None):
    """
    出题记录（按时间顺序）。

    参数:
        con: 数据库连接
        since: 起始时间字符串（含），可选
        until: 截止时间字符串（含），可选
    返回:
        list[sqlite3.Row]
    """
    rows = con.execute("SELECT * FROM served ORDER BY id").fetchall()
    return [r for r in rows if (not since or r["ts"] >= since) and (not until or r["ts"] <= until)]


def served_problems(con):
    """
    已出过的大题集合。

    参数:
        con: 数据库连接
    返回:
        set[str]
    """
    return {r["problem_id"] for r in con.execute("SELECT DISTINCT problem_id FROM served")}


def problem_history(con):
    """
    每道做过的大题的最近作答情况：最近作答时间、最近一次结果、最近结果为不会/部分会的小问。

    参数:
        con: 数据库连接
    返回:
        dict，题目 ID -> {last_ts, last, bad_keys}
    """
    latest = {}
    for r in con.execute("SELECT problem_id, subq_key, result, ts FROM attempts WHERE result != '跳过' ORDER BY id"):
        latest[(r["problem_id"], r["subq_key"])] = (r["result"], r["ts"])
    out = {}
    for (pid, key), (res, ts) in latest.items():
        h = out.setdefault(pid, {"last_ts": ts, "last": res, "bad_keys": set()})
        if ts >= h["last_ts"]:
            h["last_ts"], h["last"] = ts, res
        if res in ("不会", "部分会"):
            h["bad_keys"].add(key)
    return out


def last_activity(con, served_id):
    """
    某次出题之后最近一次作答的时间（同一道题分几次记录时，用时从上一次记录算起）。

    参数:
        con: 数据库连接
        served_id: 出题记录 ID
    返回:
        str 或 None
    """
    r = con.execute("SELECT MAX(ts) FROM attempts WHERE served_id = ?", (served_id,)).fetchone()
    return r[0] if r else None


def card_retrievability(con, at=None):
    """
    每道做过的大题的最低记忆保持率（FSRS），用于“提前复习”时先出最可能忘的题。

    参数:
        con: 数据库连接
        at: 时间（默认现在）
    返回:
        dict，题目 ID -> 0-1
    """
    out = {}
    for r in con.execute("SELECT problem_id, card FROM cards"):
        rv = review.retrievability(r["card"], to_utc(at))
        out[r["problem_id"]] = min(out.get(r["problem_id"], 1.0), rv)
    return out


def due_cards(con, at=None, concept_prefixes=None):
    """
    已到期需要复习的题，按大题汇总到期的小问（最早到期的大题在前）。

    参数:
        con: 数据库连接
        at: 时间（本地时间，默认现在）
        concept_prefixes: 只看涉及这些知识点（ID 或前缀）的小问，可选
    返回:
        list[dict]，含 problem_id、keys（到期小问）、due、last_results、reps、concept_ids、overdue_days
    """
    now = to_utc(at)
    groups = {}
    for r in con.execute("SELECT * FROM cards ORDER BY due"):
        due = datetime.fromisoformat(r["due"])
        ids = json.loads(r["concept_ids"])
        if due > now or (concept_prefixes and not any(c.startswith(p) for c in ids for p in concept_prefixes)):
            continue
        g = groups.setdefault(r["problem_id"], {"problem_id": r["problem_id"], "keys": [], "due": due,
                                                "last_results": [], "reps": 0, "concept_ids": []})
        g["keys"].append(r["subq_key"])
        g["last_results"].append(r["last_result"])
        g["reps"] = max(g["reps"], r["reps"])
        g["concept_ids"] = sorted(set(g["concept_ids"]) | set(ids))
    out = sorted(groups.values(), key=lambda g: g["due"])
    for g in out:
        g["overdue_days"] = round((now - g["due"]).total_seconds() / 86400, 1)
        g["due"] = g["due"].astimezone().strftime(TS_FMT)
    return out


def exam_day(exam_date):
    """
    考试日期转成 date：只写了年月（YYYY-MM）时按当月 1 日算。

    参数:
        exam_date: YYYY-MM-DD 或 YYYY-MM
    返回:
        datetime.date；格式不对时返回 None
    """
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(exam_date or "", fmt).date()
        except ValueError:
            pass
    return None


def set_goal(con, exam_date, universities, programs, note="", at=None):
    """
    设定当前目标（旧目标自动失效）。

    参数:
        con: 数据库连接
        exam_date: 考试日期 YYYY-MM-DD（只知道月份时 YYYY-MM）
        universities: 目标学校正式名称列表
        programs: 目标专攻关键词列表
        note: 备注
        at: 时间（默认现在）
    返回:
        int，目标 ID
    """
    con.execute("UPDATE goals SET active = 0")
    cur = con.execute("INSERT INTO goals (created, exam_date, universities, programs, note) VALUES (?,?,?,?,?)",
                      (stamp(at), exam_date, json.dumps(universities, ensure_ascii=False),
                       json.dumps(programs, ensure_ascii=False), note))
    con.commit()
    return cur.lastrowid


def active_goal(con):
    """
    当前有效目标。

    参数:
        con: 数据库连接
    返回:
        dict 或 None
    """
    r = con.execute("SELECT * FROM goals WHERE active = 1 ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    return {"exam_date": r["exam_date"], "universities": json.loads(r["universities"]),
            "programs": json.loads(r["programs"]), "note": r["note"], "created": r["created"]}


def add_period(con, name, start_date, end_date, focus, note="", at=None):
    """
    添加一个重要期间（冲刺期、复习周等）。

    参数:
        con: 数据库连接
        name: 名称
        start_date: 开始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD
        focus: 重点知识点 ID 或前缀列表
        note: 备注
        at: 时间（默认现在）
    返回:
        int，期间 ID
    """
    cur = con.execute("INSERT INTO periods (created, name, start_date, end_date, focus, note) VALUES (?,?,?,?,?,?)",
                      (stamp(at), name, start_date, end_date, json.dumps(focus, ensure_ascii=False), note))
    con.commit()
    return cur.lastrowid


def list_periods(con, at=None, include_past=False):
    """
    重要期间列表，附带状态（进行中/未开始/已结束）。

    参数:
        con: 数据库连接
        at: 时间（默认现在）
        include_past: 是否包含已结束的期间
    返回:
        list[dict]
    """
    today = (at or datetime.now()).strftime("%Y-%m-%d")
    out = []
    for r in con.execute("SELECT * FROM periods ORDER BY start_date"):
        status = "进行中" if r["start_date"] <= today <= r["end_date"] else ("未开始" if today < r["start_date"] else "已结束")
        if status == "已结束" and not include_past:
            continue
        out.append({"id": r["id"], "name": r["name"], "start_date": r["start_date"], "end_date": r["end_date"],
                    "focus": json.loads(r["focus"]), "note": r["note"], "status": status})
    return out


def add_observation(con, text, source="claude", at=None):
    """
    记录一条观察（学生的目标、偏好、情绪、卡住的方式，或学生对画像的纠正）。

    参数:
        con: 数据库连接
        text: 观察内容（一句话）
        source: claude（Claude 观察到的）或 student（学生自己说的）
        at: 时间（默认现在）
    返回:
        int，记录 ID
    """
    cur = con.execute("INSERT INTO observations (ts, text, source) VALUES (?,?,?)", (stamp(at), text, source))
    con.commit()
    return cur.lastrowid


def list_observations(con, limit=30):
    """
    最近的观察记录（新的在前）。

    参数:
        con: 数据库连接
        limit: 最多几条
    返回:
        list[dict]
    """
    return [dict(r) for r in con.execute("SELECT * FROM observations ORDER BY id DESC LIMIT ?", (limit,))]


def profile_path():
    """
    学习画像文件路径（环境变量 KAKOMON_PROFILE 可改，测试时用临时文件）。

    返回:
        str
    """
    return os.environ.get("KAKOMON_PROFILE", PROFILE_PATH)


def get_profile():
    """
    读取学习画像正文（画像解读部分）。

    返回:
        str，没有时为空字符串
    """
    if not os.path.exists(profile_path()):
        return ""
    with open(profile_path(), encoding="utf-8") as f:
        return f.read()


def save_profile(con, markdown, attempts_at, at=None):
    """
    保存学习画像（每个版本都存入历史表，并记下保存时的作答量，用于判断画像是否过期）。

    参数:
        con: 数据库连接
        markdown: 画像正文
        attempts_at: 保存时的作答记录条数
        at: 时间（默认现在）
    """
    con.execute("INSERT INTO profile_history (ts, markdown, attempts_at) VALUES (?,?,?)", (stamp(at), markdown, attempts_at))
    con.commit()
    with open(profile_path(), "w", encoding="utf-8") as f:
        f.write(markdown)


def last_profile_save(con):
    """
    最近一次保存画像的时间与当时的作答量。

    参数:
        con: 数据库连接
    返回:
        sqlite3.Row 或 None
    """
    return con.execute("SELECT ts, attempts_at FROM profile_history ORDER BY id DESC LIMIT 1").fetchone()


def report_issue(con, problem_id, description, at=None):
    """
    记录题目转写或标签错误（有未处理报错的题暂停出题）。

    参数:
        con: 数据库连接
        problem_id: 大题 ID
        description: 问题描述
        at: 时间（默认现在）
    返回:
        int，记录 ID
    """
    cur = con.execute("INSERT INTO issues (ts, problem_id, description) VALUES (?,?,?)", (stamp(at), problem_id, description))
    con.commit()
    return cur.lastrowid


def open_issue_problems(con):
    """
    有未处理报错的大题。

    参数:
        con: 数据库连接
    返回:
        set[str]
    """
    return {r[0] for r in con.execute("SELECT DISTINCT problem_id FROM issues WHERE status = 'open'")}


def list_issues(con, status="open"):
    """
    报错列表。

    参数:
        con: 数据库连接
        status: open / resolved / all
    返回:
        list[dict]
    """
    if status == "all":
        return [dict(r) for r in con.execute("SELECT * FROM issues ORDER BY id")]
    return [dict(r) for r in con.execute("SELECT * FROM issues WHERE status = ? ORDER BY id", (status,))]


def resolve_issue(con, issue_id, resolution=""):
    """
    把报错标为已处理（题目恢复出题）。

    参数:
        con: 数据库连接
        issue_id: 报错 ID
        resolution: 处理说明
    """
    con.execute("UPDATE issues SET status = 'resolved', resolution = ? WHERE id = ?", (resolution, issue_id))
    con.commit()
