"""
复习排期：用 FSRS 为每道做过的大题安排下次复习时间。

反馈映射：不会或看过完整讲解 -> Again，部分会或看提示后才会 -> Hard，会 -> Good（不用 Easy，避免间隔拉得过长）。
关闭分钟级学习步长，第一次就按天排期：不会约 1 天、部分会约 1 天、会约 2 天，之后逐次拉长；最长 180 天。
"""
import json
from datetime import datetime, timezone

from fsrs import Card, Rating, Scheduler

RATING = {"不会": Rating.Again, "部分会": Rating.Hard, "会": Rating.Good}
SCHEDULER = Scheduler(learning_steps=(), relearning_steps=(), enable_fuzzing=False, maximum_interval=180)


def utcnow():
    """
    当前 UTC 时间（带时区）。

    返回:
        datetime
    """
    return datetime.now(timezone.utc)


def rating(result, hints=0, solution_viewed=False):
    """
    把作答结果换成 FSRS 评分。

    参数:
        result: 会 / 部分会 / 不会
        hints: 看过几次提示
        solution_viewed: 作答前是否看过完整讲解
    返回:
        Rating
    """
    if solution_viewed:
        return Rating.Again
    if result == "会" and hints:
        return Rating.Hard
    return RATING[result]


def review(card_json, result, when=None, hints=0, solution_viewed=False):
    """
    根据一次作答更新卡片。

    参数:
        card_json: 原卡片 JSON（无则新建）
        result: 会 / 部分会 / 不会
        when: 作答时间（UTC，默认现在）
        hints: 看过几次提示
        solution_viewed: 作答前是否看过完整讲解
    返回:
        (str 新卡片 JSON, datetime 下次到期时间)
    """
    card = Card.from_dict(json.loads(card_json)) if card_json else Card()
    card, _ = SCHEDULER.review_card(card, rating(result, hints, solution_viewed), when or utcnow())
    return json.dumps(card.to_dict()), card.due


def retrievability(card_json, when=None):
    """
    卡片在某时刻的记忆保持率估计。

    参数:
        card_json: 卡片 JSON
        when: 时间（UTC，默认现在）
    返回:
        float，0-1
    """
    card = Card.from_dict(json.loads(card_json))
    return SCHEDULER.get_card_retrievability(card, when or utcnow())
