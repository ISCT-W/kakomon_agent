"""
跨文档挂接与查重：把解答集里的解答、勘误文档里的订正挂到对应的大题上，并找出疑似重复的大题。

挂接规则：同一文件夹（同校同专攻）＋同一入学年度＋大题主编号（「第1問 I. (平成29年度)」取 1）；
同号题不止一道时，用科目标题区分。解答挂不上或有歧义时只写进报告，不自动猜；
勘误只写了主编号而对应多道题（如 3-1～3-4）时挂到全部候选上（勘误正文自己写明了改哪一处）。查重只报告，不自动合并。
"""
import os
import re
import sys
import unicodedata
import zlib
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))
import labels  # noqa: E402

MINHASH_PERM = 64
MINHASH_BANDS = 16
DUP_THRESHOLD = 0.8
MERSENNE = (1 << 31) - 1


def group_of(rel_path):
    """
    文档所在文件夹（挂接只在同一文件夹内进行）。

    参数:
        rel_path: 文档相对路径（照片文档以“/”结尾）
    返回:
        str
    """
    return os.path.dirname(rel_path.rstrip("/"))


def norm_text(s):
    """
    比较用的规范化：NFKC、去空白与标点、小写。

    参数:
        s: 字符串
    返回:
        str
    """
    return re.sub(r"[\s、，,。.・:：()（）\[\]【】]", "", unicodedata.normalize("NFKC", s or "")).lower()


def match_key(year, label):
    """
    挂接用的键：年度＋大题主编号。

    参数:
        year: 入学年度
        label: 题号原文
    返回:
        tuple
    """
    return (year, labels.main_number(label))


def pick(cands, section):
    """
    在同号候选题中挑出唯一的一道。

    参数:
        cands: 候选大题列表
        section: 条目的科目标题
    返回:
        (大题或 None, str 失败原因)
    """
    if len(cands) == 1:
        return cands[0], ""
    if not cands:
        return None, "没有对应的题"
    sec = norm_text(section)
    if sec:
        same = [c for c in cands if norm_text(c["section"]) == sec]
        if len(same) != 1:
            same = [c for c in cands if norm_text(c["section"]) and
                    (sec in norm_text(c["section"]) or norm_text(c["section"]) in sec)]
        if len(same) == 1:
            return same[0], ""
    return None, f"有 {len(cands)} 道同号题，无法确定"


def attach(problem, field, text, source, heading=""):
    """
    把解答或勘误追加到大题上：同一来源的几段（如 I、II、III 分开的解答）按顺序接在一起，
    不同来源之间用分隔线隔开并注明来源。

    参数:
        problem: 大题
        field: solution_md 或 errata_md
        text: 要追加的 Markdown
        source: 来源文档相对路径
        heading: 加在这段前面的小标题（如解答条目的原题号）
    """
    if not text.strip():
        return
    block = f"**{heading}**\n\n{text}" if heading else text
    srcs_key = f"_{field}_sources"
    srcs = problem.setdefault(srcs_key, [])
    if not problem[field].strip():
        problem[field] = block
    elif source in srcs:
        problem[field] += f"\n\n{block}"
    else:
        problem[field] += f"\n\n---\n\n（来源：{source}）\n\n{block}"
    if source not in srcs:
        srcs.append(source)
    if field == "solution_md":
        problem["solution_source"] = "；".join(s for s in dict.fromkeys(
            [x for x in problem.get("solution_source", "").split("；") if x] + [source]))


def link(docs, doc_rows):
    """
    挂接解答与勘误。会就地修改大题，并从解答集中去掉已并入试卷的重述题面。

    参数:
        docs: dict，doc_id -> assemble.build_doc 的结果
        doc_rows: dict，doc_id -> 清单文档行
    返回:
        dict，挂接报告（各类已挂接、未挂接条目）
    """
    index = defaultdict(list)
    for doc_id, d in docs.items():
        if doc_rows[doc_id].get("document_kind") == "solutions":
            continue
        g = group_of(doc_rows[doc_id]["rel_path"])
        for p in d["problems"]:
            index[(g,) + match_key(p["admission_year"], p["label"])].append(p)
    rep = {k: [] for k in ("restated_merged", "restated_kept", "solutions_linked", "solutions_unlinked",
                           "errata_linked", "errata_unlinked")}

    def where(doc_id, item):
        return {"doc": doc_rows[doc_id]["rel_path"], "label": item["label"], "year": item["admission_year"],
                "section": item["section"], "pages": item["pages"]}

    for doc_id, d in docs.items():
        if doc_rows[doc_id].get("document_kind") != "solutions":
            continue
        g, keep = group_of(doc_rows[doc_id]["rel_path"]), []
        for p in d["problems"]:
            target, why = pick(index.get((g,) + match_key(p["admission_year"], p["label"]), []), p["section"])
            if target:
                attach(target, "solution_md", p["solution_md"], doc_rows[doc_id]["rel_path"], p["label"])
                rep["restated_merged"].append(dict(where(doc_id, p), to=target["problem_id"]))
            else:
                keep.append(p)
                rep["restated_kept"].append(dict(where(doc_id, p), reason=why))
        d["problems"] = keep

    for kind, field in (("solutions", "solution_md"), ("errata", "errata_md")):
        for doc_id, d in docs.items():
            g = group_of(doc_rows[doc_id]["rel_path"])
            for e in d[kind]:
                key = match_key(e["admission_year"], e["label"])
                own = [p for p in d["problems"] if match_key(p["admission_year"], p["label"]) == key]
                cands = own or index.get((g,) + key, [])
                targets, why = [], "没有目标题号"
                if key[1]:
                    target, why = pick(cands, e["section"])
                    targets = [target] if target else []
                    if not target and kind == "errata" and len(cands) > 1:
                        body = norm_text(e[field])
                        named = [c for c in cands if norm_text(c["section"]) and norm_text(c["section"]) in body]
                        targets = named or cands
                for target in targets:
                    attach(target, field, e[field], doc_rows[doc_id]["rel_path"], e["label"])
                if targets:
                    rep[f"{kind}_linked"].append(dict(where(doc_id, e), to=[t["problem_id"] for t in targets]))
                else:
                    rep[f"{kind}_unlinked"].append(dict(where(doc_id, e), reason=why))
    return rep


def shingles(text, n=5):
    """
    正文的 n 字片段集合（去空白、NFKC 后）。

    参数:
        text: 正文
        n: 片段长度
    返回:
        set[str]
    """
    t = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def near_duplicates(problems, threshold=DUP_THRESHOLD):
    """
    用 MinHash 分桶找出正文高度相似的大题对（只报告，不合并）。

    参数:
        problems: 大题列表（需含 problem_id、content_md）
        threshold: Jaccard 相似度阈值
    返回:
        list[dict]，含 a、b、similarity
    """
    rng = np.random.default_rng(0)
    a = rng.integers(1, MERSENNE, MINHASH_PERM, dtype=np.int64)
    b = rng.integers(0, MERSENNE, MINHASH_PERM, dtype=np.int64)
    sets, sigs = {}, {}
    for p in problems:
        sh = shingles(p["content_md"])
        if len(sh) < 20:
            continue
        h = np.array([zlib.crc32(s.encode("utf-8")) for s in sh], dtype=np.int64) % MERSENNE
        sigs[p["problem_id"]] = ((a[:, None] * h[None, :] + b[:, None]) % MERSENNE).min(axis=1)
        sets[p["problem_id"]] = sh
    rows = MINHASH_PERM // MINHASH_BANDS
    pairs = set()
    for band in range(MINHASH_BANDS):
        buckets = defaultdict(list)
        for pid, sig in sigs.items():
            buckets[tuple(sig[band * rows:(band + 1) * rows])].append(pid)
        for ids in buckets.values():
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    pairs.add(tuple(sorted((ids[i], ids[j]))))
    out = []
    for x, y in sorted(pairs):
        sim = len(sets[x] & sets[y]) / len(sets[x] | sets[y])
        if sim >= threshold:
            out.append({"a": x, "b": y, "similarity": round(sim, 3)})
    return out


def duplicate_map(problems, pairs, kinds):
    """
    把疑似重复的题对并成组（同一道题可能出现在好几处），每组保留一道：优先正式试卷，其次年度早的，再按 ID；
    其余标为重复题。

    参数:
        problems: 大题列表（需含 problem_id、admission_year）
        pairs: near_duplicates 的结果
        kinds: dict，题目 ID -> 所在文档的类型（exam_paper、sample_problems 等）
    返回:
        dict，重复题 ID -> 保留的题 ID
    """
    year = {p["problem_id"]: p["admission_year"] or 9999 for p in problems}
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for pr in pairs:
        parent[find(pr["a"])] = find(pr["b"])
    groups = defaultdict(list)
    for x in list(parent):
        groups[find(x)].append(x)
    out = {}
    for members in groups.values():
        keep = min(members, key=lambda m: (kinds.get(m) not in ("exam_paper", "single_problem"), year.get(m, 9999), m))
        out.update({m: keep for m in members if m != keep})
    return out
