"""
拼题模块：把一份文档逐页转写的片段归并成大题，并给出只有解答的条目与勘误条目；裁剪插图。

模型只根据片段摘要（页码、题号、接续标记、正文首尾）和页面上印刷的年度/科目标记决定分组，不重写正文；
正文合并、英译剔除、年度与科目的沿用、题目 ID、插图裁剪都由代码完成。大文档先分块分组再合并。
"""
import json
import os
import re
import sys
from collections import Counter

import common
from normalize_manifest import year_from_page_text

sys.path.insert(0, os.path.join(common.PROJECT_DIR, "agent"))
import labels  # noqa: E402

STRUCTURE_VERSION = "v2"
CHUNK_MAX_ROWS = 80
CHUNK_MAX_PAGES = 30
CHUNK_TARGET_PAGES = 25
CHUNK_HARD_PAGES = 40
MULTI_YEAR_KINDS = ("compilation", "solutions")

STRUCTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "segment_ids": {"type": "array", "items": {"type": "string"}},
                    "solution_segment_ids": {"type": "array", "items": {"type": "string"}},
                    "title_hint": {"type": "string"},
                },
                "required": ["label", "segment_ids", "solution_segment_ids", "title_hint"],
                "additionalProperties": False,
            },
        },
        "solutions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "segment_ids": {"type": "array", "items": {"type": "string"}}},
                "required": ["label", "segment_ids"],
                "additionalProperties": False,
            },
        },
        "errata": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"target_label": {"type": "string"},
                               "segment_ids": {"type": "array", "items": {"type": "string"}}},
                "required": ["target_label", "segment_ids"],
                "additionalProperties": False,
            },
        },
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"segment_ids": {"type": "array", "items": {"type": "string"}},
                               "translation_of": {"type": "string"}},
                "required": ["segment_ids", "translation_of"],
                "additionalProperties": False,
            },
        },
        "unassigned": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"segment_id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["segment_id", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["problems", "solutions", "errata", "translations", "unassigned"],
    "additionalProperties": False,
}

STRUCTURE_SYSTEM = """你会收到一份日本大学院入学考试文档逐页转写后的片段列表（按页序），任务是把片段归并成大题（大問）。

规则：
1. 大题是卷面上独立编号、独立作答的题，常见编号如 問題1、第1問、A-1、Problem 1、[1]、1.、【問1】。
   (1)、(a)、(i)、1)、a) 一般是大题内的小问，不能拆成独立大题。
   “問1、問2”在有的卷子里是独立大题（例如选做规则写“6問中3問”，或每个“問”是不同科目），在有的卷子里是同一大题内的小问，请结合文档说明和卷面结构判断。
2. 一道大题可能跨多页、被拆成多个片段（无题号的续页片段、接续标记为 true 的片段），要合并到同一大题，segment_ids 按页序排列。
3. 带 "marker": true 的行是页面上印刷的年度或科目标记，不是片段，不要放进任何分组。
   合订本里同一题号会在不同年度或不同科目下重复出现，它们是不同的大题，要分别列出。
4. 英文片段如果是本文档中某道日文大题的翻译，放进 translations，translation_of 填对应日文大题的 label，不要作为独立大题。整份文档只有英文时，英文大题照常列入 problems。
5. role 为 solution 的片段：本文档里有对应题面时，放进那道大题的 solution_segment_ids；
   本文档里没有对应题面（如解答集），放进 solutions，label 填它所解答的题号。
6. 勘误、订正说明的片段放进 errata，target_label 填被订正的题号（看不出时留空）。
7. 不属于任何大题的片段（多道题共用的说明、选题说明等）放进 unassigned 并用中文说明原因。
8. label 用卷面原文题号（如“A-1”“問題２”“第3問”）；title_hint 用中文概括这道大题的主题，不超过 20 字。
9. 每个片段 ID 必须且只能出现一次：problems（segment_ids 或 solution_segment_ids）、solutions、errata、translations、unassigned 之一。
10. 如果给出了“上一块最后一道大题”，本块开头没有题号且接续上一页的片段属于那道题，用同一个 label 列出。"""


def page_info(pages):
    """
    整理每页的页面级信息。

    参数:
        pages: list[dict]，每项含 page（页码，从 1 开始）与 record（页面转写记录）
    返回:
        dict，页码 -> {page_type, year_text, section_title}
    """
    info = {}
    for p in pages:
        res = p["record"].get("result") or {}
        info[p["page"]] = {"page_type": res.get("page_type", ""), "year_text": (res.get("year_text") or "").strip(),
                           "section_title": (res.get("section_title") or "").strip()}
    return info


def segment_rows(pages):
    """
    把逐页转写结果展开成带 ID 的片段列表。

    参数:
        pages: list[dict]，每项含 page 与 record
    返回:
        list[dict]，每项含 id、page、page_type、lang、segment
    """
    rows = []
    for p in pages:
        res = p["record"].get("result") or {}
        for i, seg in enumerate(res.get("segments", [])):
            rows.append({"id": f"p{p['page']:03d}s{i}", "page": p["page"], "page_type": res.get("page_type"),
                         "lang": res.get("main_language"), "segment": seg})
    return rows


def page_context(info):
    """
    按页序沿用年度与科目：每页取到该页为止最近一次印刷的年度原文和科目标题。

    参数:
        info: page_info 的结果
    返回:
        dict，页码 -> (year_text, section_title)
    """
    ctx, year, section = {}, "", ""
    for p in sorted(info):
        year = info[p]["year_text"] or year
        section = info[p]["section_title"] or section
        ctx[p] = (year, section)
    return ctx


def page_years(info):
    """
    各页印刷的年度（规范化为入学年度）出现的页数。

    参数:
        info: page_info 的结果
    返回:
        Counter，年度 -> 页数
    """
    ys = Counter(year_from_page_text(v["year_text"]) for v in info.values() if v["year_text"])
    ys.pop(0, None)
    return ys


def is_multi_year(info, doc_kind):
    """
    是否按页面年度给每道题定年度：合订本、解答集；或页面上有 3 个以上年度且没有哪个年度占到七成
    （被误标成普通试卷的合订本）。普通试卷里偶尔出现别的年度（如实施日期、引用往年）时仍用文档年度。

    参数:
        info: page_info 的结果
        doc_kind: 清单中的文档类型
    返回:
        bool
    """
    if doc_kind in MULTI_YEAR_KINDS:
        return True
    ys = page_years(info)
    return len(ys) >= 3 and max(ys.values()) / sum(ys.values()) < 0.7


def split_by_year(info, doc_kind):
    """
    分块时是否在年度变化处切开（比 is_multi_year 宽：页面上出现两个以上年度就算，保持已有分块计划不变）。

    参数:
        info: page_info 的结果
        doc_kind: 清单中的文档类型
    返回:
        bool
    """
    return doc_kind in MULTI_YEAR_KINDS or len(page_years(info)) >= 2


def plan_chunks(rows, info, multi):
    """
    大文档分块：片段太多或页数太多时，在“新大题开始”的页（合订本在年度变化处）切开。

    参数:
        rows: segment_rows 的结果
        info: page_info 的结果
        multi: is_multi_year 的结果
    返回:
        list[(首页, 末页)]，覆盖文档全部页
    """
    all_pages = sorted(info)
    if not all_pages:
        return []
    row_pages = sorted({r["page"] for r in rows})
    if len(rows) <= CHUNK_MAX_ROWS and len(row_pages) <= CHUNK_MAX_PAGES:
        return [(all_pages[0], all_pages[-1])]
    first = {}
    for r in rows:
        first.setdefault(r["page"], r["segment"])
    ctx = page_context(info)
    starts, start, prev_year = [all_pages[0]], all_pages[0], None
    for p in row_pages:
        year = year_from_page_text(ctx[p][0])
        seg = first[p]
        clean = bool(seg["problem_label"]) and not seg["continues_from_previous_page"]
        size = len([q for q in row_pages if start <= q < p])
        boundary = (multi and year and prev_year and year != prev_year) or (not multi and clean)
        if size and ((size >= CHUNK_TARGET_PAGES and boundary) or (size >= CHUNK_HARD_PAGES and clean)):
            starts.append(p)
            start = p
        prev_year = year or prev_year
    ends = [s - 1 for s in starts[1:]] + [all_pages[-1]]
    return list(zip(starts, ends))


def summary_text(rows, doc_meta, info, chunk, prev_label="", missing=()):
    """
    生成给模型的片段摘要文本（含页面上的年度/科目标记）。

    参数:
        rows: segment_rows 的结果
        doc_meta: 清单中的文档元数据
        info: page_info 的结果
        chunk: (首页, 末页)
        prev_label: 上一块最后一道大题的题号（分块时用）
        missing: 上一次分组漏掉的片段 ID（重跑时提醒模型）
    返回:
        str
    """
    lines = [f"文档：卷名 {doc_meta.get('paper_title', '')}；类型 {doc_meta.get('document_kind', '')}；"
             f"语言 {doc_meta.get('languages', '')}；选做规则 {doc_meta.get('selection_rule', '')}；"
             f"大题数提示 {doc_meta.get('problem_count_hint', 0)}"]
    if prev_label:
        lines.append(f"上一块最后一道大题：{prev_label}")
    if missing:
        lines.append(f"注意：上一次分组漏掉了这些片段，这次每个都要放进某一组：{'、'.join(missing)}")
    lines += ["", "片段列表："]
    by_page = {}
    for r in rows:
        by_page.setdefault(r["page"], []).append(r)
    for p in sorted(q for q in info if chunk[0] <= q <= chunk[1]):
        v = info[p]
        if v["year_text"] or v["section_title"]:
            lines.append(json.dumps({"marker": True, "page": p, "page_type": v["page_type"],
                                     "year_text": v["year_text"], "section_title": v["section_title"]},
                                    ensure_ascii=False))
        for r in by_page.get(p, []):
            s = r["segment"]
            md = s["content_md"].replace("\n", " ")
            lines.append(json.dumps({
                "id": r["id"], "page": r["page"], "page_type": r["page_type"], "lang": r["lang"],
                "role": s.get("role"), "label": s["problem_label"], "cont_prev": s["continues_from_previous_page"],
                "cont_next": s["continues_to_next_page"], "head": md[:250], "tail": md[-80:] if len(md) > 330 else "",
            }, ensure_ascii=False))
    return "\n".join(lines)


def chunk_request(rows, doc_meta, info, chunk, prev_label="", missing=()):
    """
    组装一块的拼题请求参数。

    参数:
        rows: segment_rows 的结果
        doc_meta: 清单中的文档元数据
        info: page_info 的结果
        chunk: (首页, 末页)
        prev_label: 上一块最后一道大题的题号
        missing: 上一次分组漏掉的片段 ID
    返回:
        dict，common.request_params 的结果
    """
    text = summary_text(rows, doc_meta, info, chunk, prev_label, missing)
    return common.request_params(STRUCTURE_SYSTEM, [{"type": "text", "text": text}], STRUCTURE_SCHEMA,
                                 effort="medium", max_tokens=16000)


def chunk_problems(rows, structure, chunk):
    """
    检查一块的分组结果：本块的片段是否都恰好归入一处。

    参数:
        rows: 整份文档的 segment_rows
        structure: 这一块的分组结果
        chunk: (首页, 末页)
    返回:
        (list[str] 漏放的片段 ID, list[str] 全部问题描述)
    """
    issues = check_structure([r for r in rows if chunk[0] <= r["page"] <= chunk[1]], structure)
    return [i.split("：", 1)[1] for i in issues if i.startswith("未归属")], issues


def last_label_before(rows, page):
    """
    某页之前最后出现的大题题号（给下一块做提示）。

    参数:
        rows: segment_rows 的结果
        page: 页码
    返回:
        str
    """
    found = ""
    for r in rows:
        if r["page"] < page and r["segment"]["problem_label"] and r["segment"].get("role") == "problem":
            found = r["segment"]["problem_label"]
    return found


def merge_chunks(chunk_structures, rows):
    """
    合并各块的分组结果；块首的大题如果从上一页接续（第一个片段标了接续），就并回上一块的最后一道大题。

    参数:
        chunk_structures: 各块的分组结果（按页序）
        rows: segment_rows 的结果
    返回:
        dict，与 STRUCTURE_SCHEMA 同形
    """
    by_id = {r["id"]: r for r in rows}
    out = {k: [] for k in ("problems", "solutions", "errata", "translations", "unassigned")}
    for st in chunk_structures:
        probs = list(st.get("problems", []))
        if probs and out["problems"] and probs[0]["segment_ids"]:
            head = by_id.get(probs[0]["segment_ids"][0])
            last = out["problems"][-1]
            if head and head["segment"]["continues_from_previous_page"]:
                last["segment_ids"] += probs[0]["segment_ids"]
                last["solution_segment_ids"] += probs[0]["solution_segment_ids"]
                probs = probs[1:]
        out["problems"] += probs
        for k in ("solutions", "errata", "translations", "unassigned"):
            out[k] += st.get(k, [])
    return out


def adopt_orphans(structure, rows, label):
    """
    兜底：整份文档一道大题都没有、却有未归属的题面片段时（如整页只有一道没有题号的编程题，被误判为续页），
    把这些片段作为一道大题。

    参数:
        structure: 合并后的分组结果
        rows: segment_rows 的结果
        label: 兜底大题的题号（用卷名）
    返回:
        dict，分组结果（没有需要兜底的情况时原样返回）
    """
    if structure["problems"]:
        return structure
    by_id = {r["id"]: r for r in rows}
    orphan = [u["segment_id"] for u in structure["unassigned"]
              if by_id.get(u["segment_id"], {}).get("segment", {}).get("role") == "problem"]
    if not orphan:
        return structure
    orphan.sort(key=lambda i: (by_id[i]["page"], i))
    return dict(structure, problems=[{"label": label, "segment_ids": orphan, "solution_segment_ids": [], "title_hint": ""}],
                unassigned=[u for u in structure["unassigned"] if u["segment_id"] not in orphan])


def check_structure(rows, structure):
    """
    检查分组是否覆盖全部片段且无重复。

    参数:
        rows: 片段列表
        structure: 分组结果
    返回:
        list[str]，问题描述（为空表示通过）
    """
    seen = []
    for p in structure["problems"]:
        seen += p["segment_ids"] + p["solution_segment_ids"]
    for k in ("solutions", "errata", "translations"):
        for t in structure.get(k, []):
            seen += t["segment_ids"]
    seen += [u["segment_id"] for u in structure["unassigned"]]
    ids = {r["id"] for r in rows}
    issues = [f"未归属：{i}" for i in sorted(ids - set(seen))]
    issues += [f"重复归属：{i}" for i in sorted({i for i in seen if seen.count(i) > 1})]
    issues += [f"不存在的片段：{i}" for i in sorted(set(seen) - ids)]
    return issues


def section_ordinals(info):
    """
    文档内各科目标题按首次出现的页序编号（标题取自转写缓存，重跑拼题时不变）。

    参数:
        info: page_info 的结果
    返回:
        dict，规范化科目标题 -> 序号（从 1 开始）
    """
    order = {}
    for p in sorted(info):
        s = norm_section(info[p]["section_title"])
        if s and s not in order:
            order[s] = len(order) + 1
    return order


def norm_section(s):
    """
    科目标题的规范化（去空白、括号、标点与“続き”字样），用于比较与编号。

    参数:
        s: 科目标题
    返回:
        str
    """
    s = labels.nfkc(s)
    s = re.sub(r"[(（]?続き[)）]?|\(cont(inued)?\.?\)", "", s, flags=re.I)
    return re.sub(r"[\s\[\]【】()（）「」『』《》:：・,，、.。]", "", s)


def year_of(page, ctx, multi, doc_year):
    """
    一道题（或解答、勘误条目）的入学年度与依据。

    参数:
        page: 该条目的第一页
        ctx: page_context 的结果
        multi: 是否按页面年度定年度
        doc_year: 文档年度
    返回:
        (int 年度, str 依据, int 页面年度)
    """
    ytext, _ = ctx.get(page, ("", ""))
    page_year = year_from_page_text(ytext) if ytext else 0
    if multi and page_year:
        return page_year, "页面", page_year
    return doc_year, "文档", page_year


def build_doc(doc_row, rows, info, structure):
    """
    按分组结果生成大题、解答条目与勘误条目，并确定年度、科目与稳定的题目 ID。
    题目 ID 用大题第一个片段在转写结果里的题号（转写有缓存，重跑拼题也不变），显示用拼题模型给的题号。

    参数:
        doc_row: 清单中的文档行（规范化后）
        rows: segment_rows 的结果
        info: page_info 的结果
        structure: 合并后的分组结果
    返回:
        dict，含 problems、solutions、errata、conflicts、multi_year
    """
    doc_id = doc_row["doc_id"]
    by_id = {r["id"]: r for r in rows}
    ctx = page_context(info)
    multi = is_multi_year(info, doc_row.get("document_kind", ""))
    doc_year = int(doc_row.get("admission_year") or 0)

    def segs(ids):
        return [by_id[i] for i in ids if i in by_id]

    problems, conflicts, bases = [], [], []
    for n, p in enumerate(structure["problems"], 1):
        body, sols = segs(p["segment_ids"]), segs(p["solution_segment_ids"])
        pages = sorted({r["page"] for r in body + sols})
        if not pages:
            continue
        year, basis, page_year = year_of(pages[0], ctx, multi, doc_year)
        if not multi and page_year and doc_year and page_year != doc_year:
            conflicts.append(f"{p['label']}：页面年度 {page_year} 与文档年度 {doc_year} 不一致，按文档年度")
        id_label = next((r["segment"]["problem_label"] for r in body if r["segment"]["problem_label"]), "") or p["label"]
        key = labels.problem_key(id_label) or f"X{n}"
        bases.append(f"{year or 'Y0'}-{key}" if multi else key)
        figures = [dict(f, page=r["page"]) for r in body for f in r["segment"]["figures"]]
        problems.append({
            "seq": n, "label": p["label"], "pkey": labels.problem_key(id_label), "title_hint": p["title_hint"],
            "section": ctx[pages[0]][1], "admission_year": year, "year_basis": basis,
            "content_md": "\n\n".join(r["segment"]["content_md"] for r in body),
            "solution_md": "\n\n".join(r["segment"]["content_md"] for r in sols),
            "solution_source": doc_row["rel_path"] if sols else "", "errata_md": "",
            "pages": pages, "figures": figures, "segment_ids": p["segment_ids"],
        })
    counts, ords = Counter(bases), section_ordinals(info)
    for i, prob in enumerate(problems):
        sec = ords.get(norm_section(prob["section"]))
        if counts[bases[i]] > 1 and sec:
            bases[i] = f"S{sec}-{bases[i]}"
    for prob, uid in zip(problems, labels.unique_keys(bases)):
        prob["problem_id"] = f"{doc_id}-{uid}"

    def entries(items, label_field, text_field):
        out = []
        for it in items:
            body = segs(it["segment_ids"])
            if not body:
                continue
            pages = sorted({r["page"] for r in body})
            year, basis, _ = year_of(pages[0], ctx, multi, doc_year)
            out.append({"label": it[label_field], "pkey": labels.problem_key(it[label_field]), "doc_id": doc_id,
                        "rel_path": doc_row["rel_path"], "section": ctx[pages[0]][1], "admission_year": year,
                        "year_basis": basis, "pages": pages,
                        text_field: "\n\n".join(r["segment"]["content_md"] for r in body)})
        return out

    return {"doc_id": doc_id, "multi_year": multi, "problems": problems, "conflicts": conflicts,
            "solutions": entries(structure.get("solutions", []), "label", "solution_md"),
            "errata": entries(structure.get("errata", []), "target_label", "errata_md")}


def crop_figures(problem, page_image_path, out_dir, pad=20):
    """
    按插图边界框从页面图像裁剪插图并保存，路径写回 figures。

    参数:
        problem: build_doc 产出的大题
        page_image_path: 函数，参数为页码，返回该页图像路径
        out_dir: 插图输出目录
        pad: 外扩像素
    """
    from PIL import Image
    os.makedirs(out_dir, exist_ok=True)
    for k, f in enumerate(problem["figures"], 1):
        bbox = f.get("bbox") or []
        f["path"] = ""
        if len(bbox) != 4 or not os.path.exists(page_image_path(f["page"])):
            continue
        img = Image.open(page_image_path(f["page"]))
        x0, y0, x1, y1 = bbox
        box = (max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad))
        if box[2] - box[0] < 10 or box[3] - box[1] < 10:
            continue
        path = os.path.join(out_dir, f"{problem['problem_id']}_f{k}.png")
        img.crop(box).save(path)
        f["path"] = path
