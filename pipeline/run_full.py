"""
数据管线主脚本：按清单把试卷 转写 → 拼题 → 挂接 → 打标签 → 入库，默认经由 Batch API（标准价一半）。

各阶段结果缓存在 data/full/（可断点续跑，后续全量直接复用），题库写入 data/bank.sqlite。
--docs 默认读 data/full/docs.txt（每行一个清单相对路径，# 开头为注释）；all 表示清单全部（扣除 data/exclude.tsv）。

用法:
    python -I pipeline/run_full.py render [--docs ...] [--workers 8]   # 并行判断有内容的页、渲染页面图像
    python -I pipeline/run_full.py estimate [--docs ...]
    python -I pipeline/run_full.py submit --stage transcribe|structure|tag [--docs ...] [--budget 125] [--sync]
    python -I pipeline/run_full.py wait         # 等待已提交的批次结束
    python -I pipeline/run_full.py collect      # 取回已结束批次的结果
    python -I pipeline/run_full.py load [--force]  # 拼题、挂接、裁图、入库（包含所有已处理完的文档）
    python -I pipeline/run_full.py qa [--out 目录]  # 生成验收报告（默认 data/full/qa/；已填写的抽检表不会被覆盖）
    python -I pipeline/run_full.py status
--sync 用同步调用代替 Batch（标准价，被安全分类器拒绝时自动换模型），用于补跑 Batch 里失败或被拒的少数请求。
"""
import argparse
import csv
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import assemble  # noqa: E402
import batch  # noqa: E402
import common  # noqa: E402
import db  # noqa: E402
import link  # noqa: E402
import tagging  # noqa: E402
import transcribe  # noqa: E402
from build_manifest import FOLDER_INFO, doc_pages, list_documents  # noqa: E402
from PIL import Image  # noqa: E402

FULL_DIR = os.path.join(common.DATA_DIR, "full")
PAGES_DIR = os.path.join(FULL_DIR, "pages")
STRUCT_DIR = os.path.join(FULL_DIR, "structure")
TAG_DIR = os.path.join(FULL_DIR, "problems")
FIG_DIR = os.path.join(FULL_DIR, "figures")
QA_DIR = os.path.join(FULL_DIR, "qa")
STATE_PATH = os.path.join(FULL_DIR, "batch_state.json")
LOAD_REPORT = os.path.join(FULL_DIR, "load_report.json")
DOCS_FILE = os.path.join(FULL_DIR, "docs.txt")
EXCLUDE = os.path.join(common.DATA_DIR, "exclude.tsv")
BANK = os.path.join(common.DATA_DIR, "bank.sqlite")
LEARNER = os.path.join(common.DATA_DIR, "learner.sqlite")
TAXONOMY = os.path.join(common.DATA_DIR, "taxonomy", "taxonomy_v1.yaml")
TAX_VERSION = tagging.taxonomy_version(TAXONOMY)
EST = {"transcribe": 0.0169, "structure": 0.0011, "tag": 0.016}
MAX_STRUCT_RETRY = 1


def load_json(path):
    """
    读取 JSON 文件，不存在时返回 None。

    参数:
        path: 文件路径
    返回:
        对象或 None
    """
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else None


def save_json(path, obj):
    """
    原子写入 JSON 文件（自动建目录）。

    参数:
        path: 文件路径
        obj: 对象
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def manifest_rows():
    """
    读取规范化后的试卷清单。清单里的专攻名有时是英文或带“修士課程”等字样，
    另加 program_folder（按目录规则得到的专攻名），找题时两者都匹配。

    返回:
        dict，rel_path -> 行（admission_year 转为整数）
    """
    rows = {}
    with open(os.path.join(common.DATA_DIR, "manifest.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            r["admission_year"] = int(r["admission_year"] or 0)
            r["program_folder"] = FOLDER_INFO.get(r["rel_path"].split("/")[0], ("", "", ""))[2]
            rows[r["rel_path"]] = r
    return rows


def excluded():
    """
    排除名单。

    返回:
        dict，rel_path -> 原因
    """
    if not os.path.exists(EXCLUDE):
        return {}
    with open(EXCLUDE, encoding="utf-8") as f:
        return {r["rel_path"]: r["reason"] for r in csv.DictReader(f, delimiter="\t")}


def select_docs(arg, rows):
    """
    确定本次处理的文档。

    参数:
        arg: --docs 参数（文件路径或 all）
        rows: 清单
    返回:
        list[str]，文档相对路径
    """
    skip = excluded()
    if arg == "all":
        return [r for r in rows if r not in skip]
    with open(arg or DOCS_FILE, encoding="utf-8") as f:
        wanted = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    unknown = [w for w in wanted if w not in rows]
    if unknown:
        raise SystemExit(f"清单里没有这些文档：{unknown}")
    return [w for w in wanted if w not in skip]


def content_pages(rel):
    """
    文档中有内容的页（结果缓存在 pages/<doc_id>/pages.json，避免重复判断）。

    参数:
        rel: 文档相对路径
    返回:
        list[dict]，含 page（从 1 开始）、path、index
    """
    doc = next(d for d in list_documents(common.KAKOMON_DIR) if d["rel_path"] == rel)
    pages, _ = doc_pages(doc)
    cache_path = os.path.join(PAGES_DIR, manifest_rows()[rel]["doc_id"], "pages.json")
    cached = load_json(cache_path)
    if cached is None or cached["total"] != len(pages):
        keep = [n for n, (path, idx) in enumerate(pages, 1) if transcribe.is_content_page(path, idx)]
        cached = {"total": len(pages), "content": keep}
        save_json(cache_path, cached)
    return [{"page": n, "path": pages[n - 1][0], "index": pages[n - 1][1]} for n in cached["content"]]


def page_paths(doc_id, page):
    """
    页面图像与转写缓存的路径。

    参数:
        doc_id: 文档 ID
        page: 页码
    返回:
        (str 图像路径, str 转写 JSON 路径)
    """
    base = os.path.join(PAGES_DIR, doc_id, f"p{page:03d}")
    return base + ".jpg", base + ".json"


def page_image(doc_id, pg):
    """
    取页面图像（没有时渲染并保存，长边 2000 像素）。

    参数:
        doc_id: 文档 ID
        pg: content_pages 的一项
    返回:
        PIL 图像
    """
    jpg, _ = page_paths(doc_id, pg["page"])
    if not os.path.exists(jpg):
        os.makedirs(os.path.dirname(jpg), exist_ok=True)
        common.page_image(pg["path"], pg["index"], transcribe.LONG_EDGE).save(jpg, format="JPEG", quality=90)
    return Image.open(jpg)


def doc_records(rel, rows):
    """
    读取一份文档全部有内容页的转写记录。

    参数:
        rel: 文档相对路径
        rows: 清单
    返回:
        list[dict] 或 None（有页面还没转写成功时）
    """
    doc_id = rows[rel]["doc_id"]
    out = []
    for pg in content_pages(rel):
        rec = load_json(page_paths(doc_id, pg["page"])[1])
        if not rec or rec.get("result") is None:
            return None
        out.append({"page": pg["page"], "record": rec})
    return out


def doc_meta(rel, rows, meta_index):
    """
    给模型的文档上下文（清单抽取结果加规范化后的文档类型）。

    参数:
        rel: 文档相对路径
        rows: 清单
        meta_index: transcribe.manifest_index 的结果
    返回:
        dict
    """
    return dict(meta_index.get(rel, {}), document_kind=rows[rel]["document_kind"])


def content_hash(problem):
    """
    大题正文的哈希（正文变化时才重新打标签）。

    参数:
        problem: 大题
    返回:
        str
    """
    return hashlib.sha1(problem["content_md"].encode("utf-8")).hexdigest()[:16]


def transcribe_items(rels, rows, meta_index):
    """
    生成还没有转写结果的页面请求。

    参数:
        rels: 文档列表
        rows: 清单
        meta_index: 清单抽取结果
    返回:
        list[dict]，batch.submit 的请求项
    """
    items = []
    for rel in rels:
        doc_id = rows[rel]["doc_id"]
        for pg in content_pages(rel):
            _, out = page_paths(doc_id, pg["page"])
            rec = load_json(out)
            if rec and rec.get("result") is not None:
                continue
            src = os.path.relpath(pg["path"], common.KAKOMON_DIR)

            def build(doc_id=doc_id, pg=pg, src=src, rel=rel):
                img = page_image(doc_id, pg)
                return transcribe.page_request(img, src, pg["index"], meta_index.get(rel, {}))
            items.append({"custom_id": f"t-{doc_id}-{pg['page']:03d}", "build": build, "out": out,
                          "meta": {"doc": rel, "page": pg["page"], "src": src}, "est": EST["transcribe"] / 2})
    return items


def structure_items(rels, rows, meta_index):
    """
    生成还没有拼题结果（或分块已变化）的文档的分块请求。

    参数:
        rels: 文档列表
        rows: 清单
        meta_index: 清单抽取结果
    返回:
        (list[dict] 请求项, list[str] 页面未转写完、暂不能拼题的文档)
    """
    items, waiting = [], []
    for rel in rels:
        pages = doc_records(rel, rows)
        if pages is None:
            waiting.append(rel)
            continue
        doc_id = rows[rel]["doc_id"]
        rows_ = assemble.segment_rows(pages)
        info = assemble.page_info(pages)
        chunks = assemble.plan_chunks(rows_, info, assemble.split_by_year(info, rows[rel]["document_kind"]))
        out = os.path.join(STRUCT_DIR, f"{doc_id}.json")
        st = load_json(out) or {}
        plan = [list(c) for c in chunks]
        if st.get("version") != assemble.STRUCTURE_VERSION or st.get("plan") != plan:
            st = {"version": assemble.STRUCTURE_VERSION, "plan": plan, "chunks": {}}
            save_json(out, st)
        if not rows_:
            empty = {k: [] for k in ("problems", "solutions", "errata", "translations", "unassigned")}
            st["chunks"] = {str(k): {"range": list(c), "structure": empty, "usage": {}, "cost": 0.0, "via": "none"}
                            for k, c in enumerate(chunks)}
            save_json(out, st)
            continue
        meta = doc_meta(rel, rows, meta_index)
        for k, ch in enumerate(chunks):
            missing = []
            done = st["chunks"].get(str(k))
            if done:
                missing, issues = assemble.chunk_problems(rows_, done["structure"], ch)
                tries = st.setdefault("retries", {}).get(str(k), 0)
                if not issues or tries >= MAX_STRUCT_RETRY:
                    continue
                st["retries"][str(k)] = tries + 1
                del st["chunks"][str(k)]
                save_json(out, st)
            prev = assemble.last_label_before(rows_, ch[0]) if k else ""

            def build(rows_=rows_, meta=meta, info=info, ch=ch, prev=prev, missing=missing):
                return assemble.chunk_request(rows_, meta, info, ch, prev, missing)
            n_pages = len([p for p in info if ch[0] <= p <= ch[1]])
            items.append({"custom_id": f"s-{doc_id}-{k:02d}", "build": build, "out": out,
                          "meta": {"doc": rel, "chunk": k, "range": list(ch)},
                          "est": max(0.002, EST["structure"] * n_pages) / 2})
    return items, waiting


def assemble_docs(rels, rows):
    """
    把拼题结果完整的文档拼成大题并挂接解答、勘误。

    参数:
        rels: 文档列表
        rows: 清单
    返回:
        (dict doc_id -> build_doc 结果, dict 挂接报告, dict doc_id -> 拼题检查问题, list 未拼完的文档)
    """
    built, checks, waiting = {}, {}, []
    for rel in rels:
        doc_id = rows[rel]["doc_id"]
        st = load_json(os.path.join(STRUCT_DIR, f"{doc_id}.json"))
        pages = doc_records(rel, rows)
        if not st or pages is None or len(st["chunks"]) < len(st["plan"]):
            waiting.append(rel)
            continue
        rows_ = assemble.segment_rows(pages)
        info = assemble.page_info(pages)
        structure = assemble.merge_chunks([st["chunks"][str(k)]["structure"] for k in range(len(st["plan"]))], rows_)
        structure = assemble.adopt_orphans(structure, rows_, rows[rel].get("paper_title") or "")
        checks[doc_id] = assemble.check_structure(rows_, structure)
        d = assemble.build_doc(rows[rel], rows_, info, structure)
        d["unassigned"] = structure["unassigned"]
        d["translations"] = len(structure["translations"])
        built[doc_id] = d
    report = link.link(built, {rows[r]["doc_id"]: rows[r] for r in rels})
    return built, report, checks, waiting


def tag_items(rels, rows, taxonomy_text):
    """
    生成还没有标签（或正文已变化）的大题的打标签请求；知识点表升级后，只重打上次报告了“表外知识点”的题。

    参数:
        rels: 文档列表
        rows: 清单
        taxonomy_text: 知识点表文本
    返回:
        (list[dict] 请求项, list[str] 拼题未完成的文档)
    """
    built, _, _, waiting = assemble_docs(rels, rows)
    by_id = {rows[r]["doc_id"]: rows[r] for r in rels}
    items = []
    for doc_id, d in built.items():
        for p in d["problems"]:
            out = os.path.join(TAG_DIR, f"{p['problem_id']}.json")
            cached = load_json(out)
            h = content_hash(p)
            if cached and cached.get("content_hash") == h and cached.get("tag_version") == tagging.TAG_VERSION and \
                    not (cached.get("taxonomy_version", "v0") != TAX_VERSION and (cached.get("tags") or {}).get("missing_concepts")):
                continue

            def build(p=p, row=by_id[doc_id]):
                return tagging.problem_request(p, row, taxonomy_text)
            items.append({"custom_id": f"g-{p['problem_id']}", "build": build, "out": out,
                          "meta": {"problem_id": p["problem_id"], "content_hash": h}, "est": EST["tag"] / 2})
    return items, waiting


def make_handler(concepts):
    """
    生成写缓存的回调（Batch 与同步调用共用）。

    参数:
        concepts: 知识点字典
    返回:
        函数 handler(stage, item, parsed, via) -> bool
    """
    def handler(stage, item, parsed, via="batch"):
        if stage == "transcribe":
            size = list(Image.open(item["out"][:-5] + ".jpg").size)
            rec = transcribe.page_record(parsed, size, None, via)
            save_json(item["out"], dict(rec, page=item["meta"]["page"], src=item["meta"]["src"]))
            return True
        if stage == "structure":
            st = load_json(item["out"])
            if not st:
                return False
            st["chunks"][str(item["meta"]["chunk"])] = {"range": item["meta"]["range"], "structure": parsed["data"],
                                                        "usage": parsed["usage"], "cost": parsed["cost"], "via": via}
            save_json(item["out"], st)
            return True
        tags = parsed["data"]
        invalid = tagging.validate(tags, concepts)
        save_json(item["out"], {"problem_id": item["meta"]["problem_id"], "content_hash": item["meta"]["content_hash"],
                                "tag_version": tagging.TAG_VERSION, "taxonomy_version": TAX_VERSION, "tags": tags,
                                "invalid_ids": invalid,
                                "usage": parsed["usage"], "cost": parsed["cost"], "via": via})
        return True
    return handler


def run_sync(client, state, stage, items, budget, handler, workers=6):
    """
    用同步调用处理请求（补跑 Batch 失败或被拒的少数请求）。

    参数:
        client: anthropic 客户端
        state: 批次状态（记录费用）
        stage: 阶段名
        items: 请求项
        budget: 预算上限
        handler: 写缓存的回调
        workers: 并发数
    返回:
        dict，含 done、failed、cost
    """
    out = {"done": 0, "failed": 0, "cost": 0.0}
    if batch.spent_total(state) + batch.pending_estimate(state) + sum(it["est"] * 2 for it in items) > budget:
        raise SystemExit("同步补跑会超出预算上限，已停止")

    def one(it):
        res = common.call_params(client, it["build"]())
        return it, res

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(one, it) for it in items]):
            it, res = fut.result()
            state["spent"][f"sync:{it['custom_id']}"] = round(res["cost"], 6)
            out["cost"] += res["cost"]
            if res["data"] is not None and handler(stage, it, res, "sync"):
                out["done"] += 1
                state["retry"].pop(it["custom_id"], None)
            else:
                out["failed"] += 1
                state["retry"][it["custom_id"]] = {"stage": stage, "out": it["out"], "meta": it["meta"],
                                                   "error": res["error"] or "handler"}
    batch.save_state(STATE_PATH, state)
    return out


def content_task(rel):
    """
    子进程任务：判断一份文档有内容的页并写入缓存。

    参数:
        rel: 文档相对路径
    返回:
        (str 文档, int 有内容页数, str 错误)
    """
    try:
        return rel, len(content_pages(rel)), ""
    except Exception as e:
        return rel, 0, repr(e)


def render_task(args):
    """
    子进程任务：渲染一页图像（已有则跳过）。

    参数:
        args: (文档 ID, content_pages 的一项)
    返回:
        str，错误说明（成功为空）
    """
    doc_id, pg = args
    try:
        page_image(doc_id, pg)
        return ""
    except Exception as e:
        return f"{doc_id} p{pg['page']}: {e!r}"


def cmd_render(rels, rows, workers):
    """
    并行判断有内容的页、渲染还没转写的页面图像（提交 Batch 前先做，提交时就不用逐页渲染）。

    参数:
        rels: 文档列表
        rows: 清单
        workers: 进程数
    """
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(workers) as pool:
        found = list(pool.map(content_task, rels, chunksize=1))
    errors = [f"{rel}: {err}" for rel, _, err in found if err]
    tasks = []
    for rel, _, err in found:
        if err:
            continue
        doc_id = rows[rel]["doc_id"]
        for pg in content_pages(rel):
            jpg, js = page_paths(doc_id, pg["page"])
            rec = load_json(js)
            if not os.path.exists(jpg) and not (rec and rec.get("result") is not None):
                tasks.append((doc_id, pg))
    with ProcessPoolExecutor(workers) as pool:
        errors += [e for e in pool.map(render_task, tasks, chunksize=8) if e]
    print(f"{len(rels)} 份文档，有内容页 {sum(n for _, n, _ in found)}，新渲染 {len(tasks)} 页；错误 {len(errors)}")
    for e in errors[:20]:
        print("  ", e)


def processed_docs(rows):
    """
    已经拼题完成的全部文档（入库与验收报告都包含它们，题库随处理进度累积）。

    参数:
        rows: 清单
    返回:
        list[str]
    """
    done = set()
    for f in os.listdir(STRUCT_DIR) if os.path.isdir(STRUCT_DIR) else []:
        if f.endswith(".json"):
            done.add(f[:-5])
    return [rel for rel, r in rows.items() if r["doc_id"] in done]


def cmd_load(rows, concepts, force):
    """
    拼题、挂接、裁图、读取标签并整库重建题库；写入入库报告。

    参数:
        rows: 清单
        concepts: 知识点字典
        force: 学习记录引用的题目缺失时也替换题库
    """
    rels = processed_docs(rows)
    built, link_rep, checks, waiting = assemble_docs(rels, rows)
    for rel in rels:
        if rows[rel]["doc_id"] in built:
            for pg in content_pages(rel):
                page_image(rows[rel]["doc_id"], pg)
    tags, untagged, invalid = {}, [], {}
    for doc_id, d in built.items():
        for p in d["problems"]:
            assemble.crop_figures(p, lambda n, d_=doc_id: page_paths(d_, n)[0], FIG_DIR)
            cached = load_json(os.path.join(TAG_DIR, f"{p['problem_id']}.json"))
            if cached and cached.get("content_hash") == content_hash(p) and cached.get("tags"):
                tags[p["problem_id"]] = cached["tags"]
                if cached.get("invalid_ids"):
                    invalid[p["problem_id"]] = cached["invalid_ids"]
            else:
                untagged.append(p["problem_id"])
    by_id = {rows[r]["doc_id"]: rows[r] for r in rels}
    all_problems = [p for d in built.values() for p in d["problems"]]
    pairs = link.near_duplicates(all_problems)
    kinds = {p["problem_id"]: by_id[doc_id]["document_kind"] for doc_id, d in built.items() for p in d["problems"]}
    duplicates = link.duplicate_map(all_problems, pairs, kinds)
    stats = db.build_bank(BANK, built, by_id, tags, concepts, common.DATA_DIR, LEARNER, force, duplicates)
    report = {
        "docs": len(built), "waiting": waiting, "stats": stats, "link": link_rep,
        "structure_issues": {k: v for k, v in checks.items() if v},
        "conflicts": {k: d["conflicts"] for k, d in built.items() if d["conflicts"]},
        "unassigned": {k: d["unassigned"] for k, d in built.items() if d["unassigned"]},
        "untagged": untagged, "invalid_concepts": invalid,
        "near_duplicates": pairs, "duplicates": duplicates,
    }
    save_json(LOAD_REPORT, report)
    print(f"入库：{len(built)} 份文档，{stats['problems']} 道大题，{stats['subquestions']} 个小问；"
          f"未打标签 {len(untagged)}；题库{'已替换' if stats['replaced'] else '未替换（学习记录引用缺失）'}")
    if stats["missing_refs"]:
        print("学习记录引用但新题库里没有：", stats["missing_refs"][:20])
    print(f"挂接：解答 {len(link_rep['solutions_linked'])}/{len(link_rep['solutions_linked']) + len(link_rep['solutions_unlinked'])}，"
          f"重述题面并入 {len(link_rep['restated_merged'])}，勘误 {len(link_rep['errata_linked'])}/"
          f"{len(link_rep['errata_linked']) + len(link_rep['errata_unlinked'])}；疑似重复 {len(pairs)} 对，"
          f"标为重复题 {len(duplicates)} 道（出题与找题时跳过）")


def cmd_status(rows, state):
    """
    打印处理进度与费用。

    参数:
        rows: 清单
        state: 批次状态
    """
    pending = [bid for bid, b in state["batches"].items() if not b["collected"]]
    pages = sum(1 for _, _, fs in os.walk(PAGES_DIR) for f in fs if f.endswith(".json") and f != "pages.json")
    structs = len(processed_docs(rows))
    tags = len(os.listdir(TAG_DIR)) if os.path.isdir(TAG_DIR) else 0
    print(f"转写缓存 {pages} 页，拼题 {structs} 份文档，标签 {tags} 道大题")
    print(f"批次 {len(state['batches'])} 个，未取回 {len(pending)} 个；已花费 ${batch.spent_total(state):.3f}，"
          f"未取回预估 ${batch.pending_estimate(state):.3f}；待补跑 {len(state['retry'])} 个")


def main():
    """
    命令行入口。
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["render", "estimate", "submit", "wait", "collect", "load", "qa", "status"])
    ap.add_argument("--stage", choices=["transcribe", "structure", "tag"])
    ap.add_argument("--docs", default="")
    ap.add_argument("--budget", type=float, default=125.0)
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default="", help="qa 的输出目录（默认 data/full/qa）")
    args = ap.parse_args()
    rows = manifest_rows()
    state = batch.load_state(STATE_PATH)
    concepts, taxonomy_text = tagging.load_taxonomy(TAXONOMY)

    if args.command == "status":
        cmd_status(rows, state)
        return
    if args.command == "qa":
        import qa_report
        print(qa_report.build(args.out or QA_DIR, BANK, LOAD_REPORT, STATE_PATH, PAGES_DIR))
        return
    if args.command == "load":
        cmd_load(rows, concepts, args.force)
        return
    client = common.make_client()
    if args.command == "wait":
        batch.wait(client, state, log=lambda s: print(s, flush=True))
        return
    if args.command == "collect":
        print(json.dumps(batch.collect(client, state, STATE_PATH, make_handler(concepts)), ensure_ascii=False, indent=1))
        return

    rels = select_docs(args.docs, rows)
    if args.command == "render":
        cmd_render(rels, rows, args.workers)
        return
    meta_index = transcribe.manifest_index()
    if args.command == "estimate":
        t = transcribe_items(rels, rows, meta_index)
        print(f"{len(rels)} 份文档；待转写 {len(t)} 页，预估 Batch ${len(t) * EST['transcribe'] / 2:.2f}"
              f"（同步 ${len(t) * EST['transcribe']:.2f}）")
        return
    if not args.stage:
        raise SystemExit("submit 需要 --stage")
    if args.stage == "transcribe":
        items, waiting = transcribe_items(rels, rows, meta_index), []
    elif args.stage == "structure":
        items, waiting = structure_items(rels, rows, meta_index)
    else:
        items, waiting = tag_items(rels, rows, taxonomy_text)
    if waiting:
        print(f"上一阶段还没完成、暂不处理的文档 {len(waiting)} 份：{waiting[:5]}")
    if not items:
        print("没有需要提交的请求")
        return
    if args.sync:
        print(json.dumps(run_sync(client, state, args.stage, items, args.budget, make_handler(concepts)),
                         ensure_ascii=False))
        return
    res = batch.submit(client, state, STATE_PATH, args.stage, items, args.budget, image=args.stage == "transcribe")
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
