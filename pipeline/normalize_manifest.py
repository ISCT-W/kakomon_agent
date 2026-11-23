"""
规范化试卷清单：统一计算“入学年度（4 月期，西历）”，标注依据与冲突，生成最终清单与汇总报告。

年度取值优先级：
1. 卷面“X年度4月期入学”；卷面只写一个“X年度”时取该年度
2. 卷面实施日期（6 月及以后的考试对应次年 4 月入学）
3. 东工卷角“NN 大修”编号（两位数为平成 NN 年度）
4. 目录规则（京大系统 examNN、北大 L4/L5/2023、东大各专攻文件名年份）
5. 模型按文件名或推断给出的年份

用法:
    python -I pipeline/normalize_manifest.py
输出:
    data/manifest.csv          最终清单（UTF-8 BOM）
    data/manifest_summary.md   汇总报告
"""
import csv
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

ERA_BASE = {"昭和": 1925, "平成": 1988, "令和": 2018}
YEAR_PAT = r"(?:(昭和|平成|令和)\s*(元|\d{1,2})|((?:19|20)\d{2}))"

FOLDER_RULES = [
    (r"^京大系统过去问/.*/exam(\d{2})(?:_\d|_math)?\.pdf$", lambda m: 1988 + int(m.group(1))),
    (r"^京大系统过去问/.*/exam(20\d{2})(?:_math)?\.pdf$", lambda m: int(m.group(1))),
    (r"^北海道大学情报学/L4", lambda m: 2022),
    (r"^北海道大学情报学/L5", lambda m: 2023),
    (r"^北海道大学情报学/2023", lambda m: 2024),
    (r"^东大電子情報学/denjo(\d{4})", lambda m: int(m.group(1))),
    (r"^东大コンピュータ科学/(\d{4})computer", lambda m: int(m.group(1))),
    (r"^东大情理システム情報学/(\d{4})system", lambda m: int(m.group(1))),
]


def nfkc(s):
    """
    全角转半角等 Unicode 规范化。

    参数:
        s: 字符串
    返回:
        str
    """
    return unicodedata.normalize("NFKC", s or "")


def to_year(era, num, western):
    """
    把（年号, 数字）或四位西历转成西历整数。

    参数:
        era: 年号或 None
        num: 年号内年份（含“元”）或 None
        western: 四位西历字符串或 None
    返回:
        int
    """
    if western:
        return int(western)
    return ERA_BASE[era] + (1 if num == "元" else int(num))


def year_from_fiscal_text(text):
    """
    从卷面“〇〇年度”原文中取 4 月期入学年度。

    参数:
        text: fiscal_year_text
    返回:
        (int 年度或 0, str 依据)
    """
    t = re.sub(r"\(\s*(?:19|20)\d{2}\s*\)", "", nfkc(text))
    m = re.search(YEAR_PAT + r"\s*年度?\s*4\s*月", t)
    if m:
        return to_year(*m.groups()), "卷面4月期入学"
    years = [to_year(*g) for g in re.findall(YEAR_PAT + r"\s*年度", t)]
    if len(years) == 1:
        return years[0], "卷面年度"
    if len(years) > 1:
        return max(years), "卷面多个年度取较晚者"
    return 0, ""


def year_from_page_text(text):
    """
    从页面上印刷的年度或实施日期原文推出 4 月期入学年度（拼题时给合订本的每道题定年度）。

    参数:
        text: 转写结果的 year_text，如「平成26年度」「2019年8月実施」「AY 2014」
    返回:
        int，未识别为 0
    """
    year, _ = year_from_fiscal_text(text)
    if year:
        return year
    t = nfkc(text)
    m = re.search(YEAR_PAT + r"\s*年\s*(\d{1,2})\s*月", t)
    if m:
        y = to_year(m.group(1), m.group(2), m.group(3))
        return y + 1 if int(m.group(4)) >= 6 else y
    m = re.search(r"(?:AY|School Year|Academic Year|Fiscal Year)\s*((?:19|20)\d{2})", t, re.I) or \
        re.search(r"((?:19|20)\d{2})\s*(?:School Year|Academic Year)", t, re.I)
    return int(m.group(1)) if m else 0


def year_from_exam_date(llm):
    """
    由实施年月推算 4 月期入学年度。

    参数:
        llm: 模型抽取结果
    返回:
        int，未知为 0
    """
    y, m = llm.get("exam_year") or 0, llm.get("exam_month") or 0
    if not y or not m:
        return 0
    return y + 1 if m >= 6 else y


def year_from_daishu(llm):
    """
    识别东工卷角的“NN 大修”编号。

    参数:
        llm: 模型抽取结果
    返回:
        int，未识别为 0
    """
    text = nfkc(" ".join([llm.get("fiscal_year_text", ""), llm.get("notes", "")]))
    m = re.search(r"(\d{2,4})\s*大修", text)
    if not m:
        return 0
    n = int(m.group(1))
    if n >= 1900:
        return n
    return 1988 + n if 10 <= n <= 31 else 0


def year_from_folder(rel_path):
    """
    按目录规则从路径推出入学年度。

    参数:
        rel_path: 相对路径
    返回:
        int，未匹配为 0
    """
    for pat, fn in FOLDER_RULES:
        m = re.search(pat, rel_path)
        if m:
            return fn(m)
    return 0


def canonical(rec):
    """
    计算一份文档的标准入学年度、实施年月、依据与冲突说明。

    参数:
        rec: manifest_llm.jsonl 中的一条记录
    返回:
        dict，含 admission_year、exam_held、year_basis、year_conflict
    """
    llm = rec.get("llm") or {}
    printed, basis = year_from_fiscal_text(llm.get("fiscal_year_text", ""))
    by_date = year_from_exam_date(llm)
    conflict = ""
    if printed and by_date and printed != by_date:
        conflict = f"卷面年度 {printed} 与实施日期推算的 {by_date} 不一致，以卷面为准"
    year = printed or by_date
    if not printed and by_date:
        basis = "实施日期推算"
    if not year:
        year, basis = year_from_daishu(llm), "东工大修编号"
    if not year:
        year, basis = year_from_folder(rec["rel_path"]), "目录规则"
    if not year and llm.get("fiscal_year"):
        year, basis = llm["fiscal_year"], "模型依据" + {"filename": "文件名", "inferred": "推断"}.get(llm.get("year_source"), "其他")
    if not year:
        basis = "未知"
    ey, em = llm.get("exam_year") or 0, llm.get("exam_month") or 0
    held = f"{ey}-{em:02d}" if ey and em else (str(ey) if ey else "")
    return {"admission_year": year, "exam_held": held, "year_basis": basis, "year_conflict": conflict}


def dup_key(rec, can):
    """
    生成“是否同一套卷”的分组键。

    参数:
        rec: 原始记录
        can: canonical() 结果
    返回:
        tuple 或 None（非整卷或年度未知时不参与分组）
    """
    llm = rec.get("llm") or {}
    if llm.get("document_kind") != "exam_paper" or not can["admission_year"]:
        return None
    title = re.sub(r"\s+", "", nfkc(llm.get("paper_title")))
    return (os.path.dirname(rec["rel_path"].rstrip("/")), nfkc(llm.get("program")), can["admission_year"],
            can["exam_held"], title)


def main():
    """
    读取原始抽取结果，写出最终清单与汇总报告。
    """
    recs = [json.loads(line) for line in open(os.path.join(common.DATA_DIR, "manifest_llm.jsonl"), encoding="utf-8")]
    recs.sort(key=lambda r: r["rel_path"])
    rows = []
    for r in recs:
        llm = r.get("llm") or {}
        can = canonical(r)
        rows.append({
            "doc_id": r["doc_id"], "rel_path": r["rel_path"], "kind": r["kind"], "pages": r["pages"],
            "text_layer_pages": r["text_layer_pages"], "university": r["folder_university"],
            "graduate_school": r["folder_school"], "program": llm.get("program") or r["folder_program"],
            "admission_year": can["admission_year"], "exam_held": can["exam_held"],
            "year_basis": can["year_basis"], "year_conflict": can["year_conflict"],
            "document_kind": llm.get("document_kind", ""), "paper_title": llm.get("paper_title", ""),
            "paper_category": llm.get("paper_category", ""), "languages": llm.get("languages", ""),
            "problem_count_hint": llm.get("problem_count_hint", 0), "selection_rule": llm.get("selection_rule", ""),
            "fiscal_year_text": llm.get("fiscal_year_text", ""), "exam_date_text": llm.get("exam_date_text", ""),
            "notes": llm.get("notes", ""), "error": r.get("error", ""),
            "_key": dup_key(r, can),
        })
    fields = [k for k in rows[0] if not k.startswith("_")]
    with open(os.path.join(common.DATA_DIR, "manifest.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    write_summary(rows, recs)


def write_summary(rows, recs):
    """
    写出 Markdown 汇总：总量、各目录概况、年份依据、冲突、疑似重复。

    参数:
        rows: 清单行
        recs: 原始记录（用于费用统计）
    """
    cost = sum(r.get("cost", 0) for r in recs)
    kinds = Counter(r["document_kind"] or "error" for r in rows)
    basis = Counter(r["year_basis"] for r in rows)
    lines = ["# 试卷清单汇总", "",
             f"- 文档 {len(rows)} 份，页数合计 {sum(r['pages'] for r in rows)}，清单抽取费用 ${cost:.2f}",
             "- 文档类型：" + "，".join(f"{k} {v}" for k, v in kinds.most_common()),
             "- 年度依据：" + "，".join(f"{k} {v}" for k, v in basis.most_common()), "",
             "## 各目录概况", "", "| 目录 | 份数 | 页数 | 入学年度范围 | 语言 | 卷别 |", "|---|---|---|---|---|---|"]
    by_folder = defaultdict(list)
    for r in rows:
        by_folder[r["rel_path"].split("/")[0]].append(r)
    fmt = lambda c: " ".join(f"{k}{v}" for k, v in c.most_common())
    for folder in sorted(by_folder):
        rs = by_folder[folder]
        ys = sorted(r["admission_year"] for r in rs if r["admission_year"])
        yr = f"{ys[0]}–{ys[-1]}" if ys else "未知"
        lines.append(f"| {folder} | {len(rs)} | {sum(r['pages'] for r in rs)} | {yr} | "
                     f"{fmt(Counter(r['languages'] for r in rs))} | {fmt(Counter(r['paper_category'] for r in rs))} |")
    conflicts = [r for r in rows if r["year_conflict"]]
    lines += ["", f"## 年份冲突（{len(conflicts)} 份，已按卷面年度处理）", ""]
    lines += [f"- `{r['rel_path']}`：{r['year_conflict']}" for r in conflicts] or ["无。"]
    groups = defaultdict(list)
    for r in rows:
        if r["_key"]:
            groups[r["_key"]].append(r)
    dups = [v for v in groups.values()
            if len(v) > 1 and max(r["pages"] for r in v) - min(r["pages"] for r in v) <= 2
            and len({r["languages"] for r in v}) == 1]
    lines += ["", f"## 疑似同一套卷（{len(dups)} 组；条件：同目录、同专攻、同年度、同卷名、页数相差≤2、语言相同）", ""]
    for v in dups:
        lines.append("- " + "；".join(f"`{r['rel_path']}`（{r['pages']} 页，{r['languages']}）" for r in v))
    if not dups:
        lines.append("无。")
    unknown = [r for r in rows if not r["admission_year"]]
    lines += ["", f"## 入学年度未知（{len(unknown)} 份）", ""]
    lines += [f"- `{r['rel_path']}`（{r['document_kind']}）：{r['notes'][:60]}" for r in unknown] or ["无。"]
    with open(os.path.join(common.DATA_DIR, "manifest_summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"docs={len(rows)} conflicts={len(conflicts)} dup_groups={len(dups)} unknown={len(unknown)} basis={dict(basis)}")


if __name__ == "__main__":
    main()
