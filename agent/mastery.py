"""
掌握度模型：每个知识点一个 Elo 式能力值 theta，由全部作答记录（每个小问一条）按时间顺序重放得到，不单独存状态。

- 小问难度 d（1-5）映射为题目参数 b = SCALE * (d - 3)；做对的概率 P = sigmoid(theta - b)
- 每条作答：theta += K(n) * w * m * (s - P)
  s：得分（会 1、部分会 0.5、不会 0；看提示后才会打折，看过完整讲解最多 0.25）；
  w：主知识点 1.0、次知识点 0.4；m：未经批改的自报“会”打折（折扣按该学生自报与批改的偏差个性化）；
  K 随该知识点累计次数 n 递减；独立或看提示做对（会，没看完整讲解）时 theta 不下降，免得做对了掌握度反而变低
- 掌握度 = 面对难度 3 的小问做对的概率 sigmoid(theta)
- 当前掌握度：正的能力值按距上次练习的时间衰减（半衰期 10 天起，每多练一次翻倍，最长 120 天）；负的不衰减
"""
import json
import math
from datetime import datetime

SCALE = 0.8
K0 = 1.0
K_DECAY = 0.2
TARGET_P = 0.7
RESULT_SCORE = {"会": 1.0, "部分会": 0.5, "不会": 0.0}
HINT_FACTOR = {0: 1.0, 1: 0.75}
HINT_FACTOR_MANY = 0.6
SOLUTION_CAP = 0.25
SELF_REPORT_DEFAULT = 0.7
SELF_REPORT_MIN_SAMPLES = 5
HALF_LIFE_DAYS = 10
HALF_LIFE_MAX = 120
TS_FMT = "%Y-%m-%d %H:%M:%S"


def sigmoid(x):
    """
    Logistic 函数。

    参数:
        x: 实数
    返回:
        float
    """
    return 1.0 / (1.0 + math.exp(-x))


def item_b(difficulty):
    """
    难度等级到题目参数的映射。

    参数:
        difficulty: 难度 1-5（缺失按 3）
    返回:
        float
    """
    return SCALE * ((difficulty or 3) - 3)


def p_correct(theta, difficulty):
    """
    能力为 theta 时做对某难度小问的概率。

    参数:
        theta: 能力值
        difficulty: 难度 1-5
    返回:
        float
    """
    return sigmoid(theta - item_b(difficulty))


def attempt_score(result, hints=0, solution_viewed=False):
    """
    一条作答的得分：结果分乘以提示折扣；看过完整讲解时最多 SOLUTION_CAP。

    参数:
        result: 会 / 部分会 / 不会 / 跳过
        hints: 看过几次提示
        solution_viewed: 作答前是否看过完整讲解
    返回:
        float；跳过为 None
    """
    if result not in RESULT_SCORE:
        return None
    s = RESULT_SCORE[result] * HINT_FACTOR.get(int(hints or 0), HINT_FACTOR_MANY)
    if solution_viewed:
        s = min(s, SOLUTION_CAP)
    return round(s, 3)


def self_report_factor(attempts):
    """
    未经批改的自报“会”的折扣：经批改的作答中，学生自报“会”而批改结果不是“会”的比例越高，折扣越大。
    样本不足时用默认值。

    参数:
        attempts: 作答记录（需含 verified、self_report、result）
    返回:
        float，0.5-1.0
    """
    checked = [a for a in attempts if a["verified"] and a["self_report"] == "会"]
    if len(checked) < SELF_REPORT_MIN_SAMPLES:
        return SELF_REPORT_DEFAULT
    over = sum(1 for a in checked if a["result"] != "会") / len(checked)
    return round(max(0.5, min(1.0, 1.0 - over)), 2)


def replay(attempts, until=None):
    """
    按时间顺序重放作答记录，得到每个知识点的能力值、累计作答量与最近练习时间。

    参数:
        attempts: 作答记录列表（每个小问一条，含 ts、result、score、solution_viewed、difficulty、concept_weights、verified、self_report）
        until: 只重放该时间（字符串，含）之前的记录
    返回:
        dict，知识点 -> {theta, peak, n, events, last_ts}
    """
    rows = [a for a in attempts if not until or a["ts"] <= until]
    factor = self_report_factor(rows)
    state = {}
    for a in rows:
        if a["result"] not in RESULT_SCORE:
            continue
        s = a["score"] if a["score"] is not None else RESULT_SCORE[a["result"]]
        m = factor if a["result"] == "会" and not a["verified"] else 1.0
        solved = a["result"] == "会" and not a["solution_viewed"]
        for c, w in json.loads(a["concept_weights"]).items():
            st = state.setdefault(c, {"theta": 0.0, "peak": 0.0, "n": 0.0, "events": 0, "last_ts": ""})
            k = K0 / (1.0 + K_DECAY * st["n"])
            step = k * w * m * (s - p_correct(st["theta"], a["difficulty"]))
            st["theta"] += max(0.0, step) if solved else step
            st["peak"] = max(st["peak"], st["theta"])
            st["n"] += w
            st["events"] += 1
            st["last_ts"] = a["ts"]
    return state


def days_between(ts, at=None):
    """
    某个时间戳到现在（或 at）的天数。

    参数:
        ts: 时间字符串
        at: 本地时间 datetime（默认现在）
    返回:
        float
    """
    if not ts:
        return 0.0
    return max(0.0, ((at or datetime.now()) - datetime.strptime(ts, TS_FMT)).total_seconds() / 86400)


def current_theta(st, at=None):
    """
    按遗忘衰减后的当前能力值（只衰减正值，向“未接触”回落）。

    参数:
        st: replay 结果中某知识点的状态
        at: 本地时间 datetime（默认现在）
    返回:
        float
    """
    if st["theta"] <= 0 or not st["last_ts"]:
        return st["theta"]
    half = min(HALF_LIFE_MAX, HALF_LIFE_DAYS * 2 ** max(0, st["events"] - 1))
    return st["theta"] * 2 ** (-days_between(st["last_ts"], at) / half)


def level(p, n):
    """
    把掌握概率换成文字等级。

    参数:
        p: 掌握概率
        n: 累计作答量（加权）
    返回:
        str：未接触 / 薄弱 / 进步中 / 掌握
    """
    if n < 0.5:
        return "未接触"
    if p >= 0.7 and n >= 1.5:
        return "掌握"
    if p >= 0.4:
        return "进步中"
    return "薄弱"


def summary(state, concept_id, at=None):
    """
    某个知识点的掌握情况摘要。

    参数:
        state: replay 的结果
        concept_id: 知识点 ID
        at: 本地时间 datetime（默认现在）
    返回:
        dict，含 mastery（当前，已衰减）、mastery_peak（历史最高）、n、events、level、days_since
    """
    st = state.get(concept_id, {"theta": 0.0, "peak": 0.0, "n": 0.0, "events": 0, "last_ts": ""})
    p = sigmoid(current_theta(st, at))
    return {"mastery": round(p, 2), "mastery_peak": round(sigmoid(st["peak"]), 2), "n": round(st["n"], 1),
            "events": st["events"], "level": level(p, st["n"]),
            "days_since": round(days_between(st["last_ts"], at), 1) if st["last_ts"] else None}


def predicted_success(state, concept_ids, difficulty, at=None):
    """
    预测做对某小问的概率（用当前掌握度；多个知识点取平均）。

    参数:
        state: replay 的结果
        concept_ids: 小问的主知识点列表
        difficulty: 小问难度
        at: 本地时间 datetime（默认现在）
    返回:
        float
    """
    ps = [p_correct(current_theta(state[c], at) if c in state else 0.0, difficulty) for c in concept_ids] or [0.5]
    return sum(ps) / len(ps)


def experience(state, concept_prefixes):
    """
    目标知识点上的累计作答量，用于判断是否“刚开始学”。

    参数:
        state: replay 的结果
        concept_prefixes: 知识点 ID 或前缀列表
    返回:
        float
    """
    return sum(st["n"] for c, st in state.items() if any(c.startswith(p) for p in concept_prefixes))
