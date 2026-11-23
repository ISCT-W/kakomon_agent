"""
Batch API 工具：分批提交请求、记录批次状态、取回结果、按预算上限停止提交。

状态文件记录每个批次的请求（custom_id -> 结果缓存路径与元数据）、预估费用、是否已取回；
费用账本记录每条结果的实际费用（Batch 按标准价一半计）。同一状态文件支持断点续跑。
"""
import json
import os
import time
from datetime import datetime

import common

MAX_IMAGE_REQUESTS = 400
MAX_TEXT_REQUESTS = 2000
MAX_BATCH_BYTES = 200 * 1024 * 1024


def load_state(path):
    """
    读取批次状态（不存在时返回空状态）。

    参数:
        path: 状态文件路径
    返回:
        dict，含 batches、retry、spent
    """
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return {"batches": {}, "retry": {}, "spent": {}}


def save_state(path, state):
    """
    原子写入批次状态。

    参数:
        path: 状态文件路径
        state: 状态
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def spent_total(state):
    """
    已取回结果的实际费用合计。

    参数:
        state: 状态
    返回:
        float
    """
    return sum(state["spent"].values())


def pending_estimate(state):
    """
    已提交但尚未取回的批次的预估费用合计。

    参数:
        state: 状态
    返回:
        float
    """
    return sum(b["est_cost"] for b in state["batches"].values() if not b["collected"])


def in_flight(state):
    """
    已提交、尚未取回的请求 ID 集合（避免重复提交）。

    参数:
        state: 状态
    返回:
        set[str]
    """
    return {cid for b in state["batches"].values() if not b["collected"] for cid in b["items"]}


def submit(client, state, state_path, stage, items, budget, image=False):
    """
    分批提交请求；每批提交前检查预算（已花费＋未取回批次预估＋本批预估不超过上限）。

    参数:
        client: anthropic 客户端
        state: 状态（会被修改并保存）
        state_path: 状态文件路径
        stage: 阶段名（transcribe / structure / tag）
        items: list[dict]，每项含 custom_id、build（无参函数，返回请求参数，提交时才调用以节省内存）、
               out（结果缓存路径）、meta、est（预估费用，Batch 价）
        budget: 预算上限（美元）
        image: 请求是否含图像（决定每批数量）
    返回:
        dict，含 submitted（请求数）、batches（批次 ID 列表）、stopped（因预算停止时的说明）
    """
    busy = in_flight(state)
    items = [it for it in items if it["custom_id"] not in busy]
    limit = MAX_IMAGE_REQUESTS if image else MAX_TEXT_REQUESTS
    out = {"submitted": 0, "batches": [], "stopped": ""}
    i = 0
    while i < len(items):
        chunk, size = [], 0
        while i < len(items) and len(chunk) < limit:
            items[i]["params"] = items[i]["build"]()
            n = len(json.dumps(items[i]["params"]))
            if chunk and size + n > MAX_BATCH_BYTES:
                break
            chunk.append(items[i])
            size += n
            i += 1
        est = sum(it["est"] for it in chunk)
        committed = spent_total(state) + pending_estimate(state)
        if committed + est > budget:
            out["stopped"] = (f"预算上限 ${budget:.2f}：已花费＋待取回 ${committed:.2f}，本批预估 ${est:.2f}，"
                              f"剩余 {len(items) - i + len(chunk)} 个请求未提交")
            break
        batch = client.messages.batches.create(
            requests=[{"custom_id": it["custom_id"], "params": it["params"]} for it in chunk])
        for it in chunk:
            it.pop("params", None)
        state["batches"][batch.id] = {
            "stage": stage, "created": batch.created_at.isoformat(timespec="seconds"), "ended": "", "collected": False,
            "est_cost": round(est, 4), "items": {it["custom_id"]: {"out": it["out"], "meta": it["meta"]} for it in chunk},
        }
        save_state(state_path, state)
        out["submitted"] += len(chunk)
        out["batches"].append(batch.id)
    return out


def wait(client, state, interval=30, log=print):
    """
    等待所有未取回的批次处理结束（定期查询状态）。

    参数:
        client: anthropic 客户端
        state: 状态
        interval: 查询间隔（秒）
        log: 输出函数
    返回:
        dict，批次 ID -> 结束时的请求计数
    """
    pending = [bid for bid, b in state["batches"].items() if not b["collected"]]
    done = {}
    while pending:
        for bid in list(pending):
            b = client.messages.batches.retrieve(bid)
            if b.processing_status == "ended":
                done[bid] = b.request_counts.model_dump() if hasattr(b.request_counts, "model_dump") else {}
                pending.remove(bid)
                log(f"{datetime.now():%H:%M:%S} 批次 {bid} 结束：{done[bid]}")
        if pending:
            time.sleep(interval)
    return done


def collect(client, state, state_path, handler):
    """
    取回已结束批次的结果。成功的交给 handler 写缓存；被拒、超长、JSON 解析失败的放进 retry；
    出错、过期、取消的不写缓存（下次提交时自动重交）。

    参数:
        client: anthropic 客户端
        state: 状态（会被修改并保存）
        state_path: 状态文件路径
        handler: 函数 handler(stage, item, parsed)，parsed 为 common.parse_message 的结果，返回是否写入成功
    返回:
        dict，按阶段统计 succeeded / retry / failed，以及各批次的耗时
    """
    summary = {"succeeded": 0, "retry": 0, "failed": 0, "batches": []}
    for bid, b in state["batches"].items():
        if b["collected"]:
            continue
        info = client.messages.batches.retrieve(bid)
        if info.processing_status != "ended":
            continue
        cost = 0.0
        for r in client.messages.batches.results(bid):
            item = b["items"].get(r.custom_id)
            if not item:
                continue
            if r.result.type != "succeeded":
                summary["failed"] += 1
                continue
            parsed = common.parse_message(r.result.message, batch=True)
            cost += parsed["cost"]
            if parsed["data"] is not None and handler(b["stage"], item, parsed):
                summary["succeeded"] += 1
            else:
                state["retry"][r.custom_id] = {"stage": b["stage"], "out": item["out"], "meta": item["meta"],
                                               "error": parsed["error"] or "handler"}
                summary["retry"] += 1
        b["collected"] = True
        b["ended"] = info.ended_at.isoformat(timespec="seconds") if info.ended_at else ""
        b["actual_cost"] = round(cost, 4)
        state["spent"][bid] = round(cost, 6)
        summary["batches"].append({"id": bid, "stage": b["stage"], "created": b["created"], "ended": b["ended"],
                                   "requests": len(b["items"]), "cost": round(cost, 4)})
        save_state(state_path, state)
    return summary
