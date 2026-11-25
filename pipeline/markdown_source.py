"""
Markdown 入口：把已经是文字的过去问（一题一个 Markdown 文件，如 The Kai Project）接入管线，不经转写与拼题模型。

来源目录放在 kakomon/ 下，目录里有 index.csv（列：path、university、graduate_school、program、admission_year、
exam_held、paper_title；path 相对该目录）。每个文件当作一份只有一页、一道大题的文档：
`## Description` 一节是题面（有 ----- 分隔的日英两版时只留日文），之后除 Author、Reference 以外的各节是解答。
图片换成占位符 [图k]，原图作为插图（SVG 转 PNG），入库时复制到 data/full/figures/。

sync() 写出清单 data/manifest_md.csv、页面缓存 pages/<doc_id>/ 与拼题缓存 structure/<doc_id>.json（费用为 0），
之后照常 submit --stage tag → wait → collect → load。
"""
import csv
import json
import os
import re

import assemble
import common
from build_manifest import doc_id

MANIFEST = os.path.join(common.DATA_DIR, "manifest_md.csv")
FIELDS = ["doc_id", "rel_path", "kind", "university", "graduate_school", "program", "program_folder",
          "admission_year", "exam_held", "document_kind", "paper_title", "paper_category", "languages",
          "selection_rule"]
SOLUTION_SKIP = ("author", "reference")
IMG = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)[^)]*\)|<img[^>]*?src="([^"]+)"[^>]*>')
KANA = re.compile(r"[぀-ヿ]")
DROP_HEAD = re.compile(r"^(English|题目描述|題意の要約|中文|翻[訳译])", re.I)
KEEP_HEAD = re.compile(r"^日本語")
LABEL = re.compile(r"^(第?[0-9０-９IVXⅠ-Ⅻ]+問?|大?問題?[0-9０-９A-ZⅠ-Ⅻ][-0-9０-９A-Za-zⅠ-Ⅻ.()（）]*|[\[［【][^\]］】]+[\]］】]"
                   r"|[A-Z]-?[0-9]+|Q\.?[0-9]+|[0-9]+(\.[0-9]+)*|[a-z]?[0-9]+)$")


def is_markdown(rel):
    """
    是否为 Markdown 来源的文档。

    参数:
        rel: 文档相对路径
    返回:
        bool
    """
    return rel.endswith(".md")


def sources(root=common.KAKOMON_DIR):
    """
    找出 kakomon/ 下带 index.csv 的 Markdown 来源目录。

    参数:
        root: kakomon 目录
    返回:
        list[str]，来源目录绝对路径
    """
    return sorted(os.path.join(root, d) for d in os.listdir(root)
                  if os.path.isfile(os.path.join(root, d, "index.csv")))


def sections(text):
    """
    按二级标题切分 Markdown（去掉 front matter 和一级标题）。

    参数:
        text: 文件内容
    返回:
        list[(str 标题, str 正文)]
    """
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    out, head, buf = [], "", []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            out.append((head, "\n".join(buf).strip()))
            head, buf = re.sub(r"[*_]", "", m.group(1)).strip(), []
        elif not line.startswith("# "):
            buf.append(line)
    out.append((head, "\n".join(buf).strip()))
    return [(h, b) for h, b in out if h or b]


def clean(md):
    """
    去掉渲染时会原样显示的 HTML 标签（保留标签内文字）。

    参数:
        md: Markdown 文本
    返回:
        str
    """
    md = re.sub(r"</?(center|div|span|br|p|details|summary|font|b|u|sup|sub)\b[^>]*>", "", md, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def japanese_part(md):
    """
    只留日文原题：去掉“### English”“### 题目描述”“### 題意の要約”等译文/概要小节和原题出处链接行；
    有 ----- 分隔的日英两版时只留含假名的部分。

    参数:
        md: 题面
    返回:
        str
    """
    kept, keep = [], True
    for block in re.split(r"^(?=###\s)", md, flags=re.M):
        head = re.match(r"###\s+(.+)", block)
        name = head.group(1).strip() if head else ""
        if DROP_HEAD.match(name):
            keep = False
            continue
        if KEEP_HEAD.match(name):
            keep = True
            block = block[head.end():]
        if keep:
            kept.append(block)
    link = r"^\s*\[[^\]]*\]\(https?://[^)]*\)\s*$"
    text = re.sub(link, "", "".join(kept), flags=re.M).strip()
    md = text or re.sub(link, "", md.split("### 题目描述")[0], flags=re.M).strip()
    parts = [p.strip() for p in re.split(r"^\s*-{5,}\s*$", md, flags=re.M) if p.strip()]
    ja = [p for p in parts if KANA.search(p)]
    return "\n\n".join(ja) if ja and len(ja) < len(parts) else md


def localize(md, base_dir, source_dir, start=1, placeholder=True):
    """
    把图片引用换成占位符（题面）或去掉（解答），返回本地图片路径。

    参数:
        md: Markdown 文本
        base_dir: 该文件所在目录（相对引用用）
        source_dir: 来源目录（网址图片已下载到 assets/<主机>/<路径>）
        start: 占位符起始编号
        placeholder: True 写 [图k]，False 写“（图略）”
    返回:
        (str 新文本, list[dict] 插图，含 src、description)
    """
    figs = []

    def repl(m):
        alt, url = (m.group(1) or ""), (m.group(2) or m.group(3))
        if url.startswith("http"):
            path = os.path.join(source_dir, "assets", re.sub(r"^https?://", "", url))
        else:
            path = os.path.normpath(os.path.join(base_dir, url))
        if not os.path.exists(path):
            return f"（图：{alt}）" if alt else "（图略）"
        if not placeholder:
            return "（图略）"
        figs.append({"src": os.path.relpath(path, common.PROJECT_DIR), "description": alt.strip()[:80]})
        return f"[图{start + len(figs) - 1}]"
    return IMG.sub(repl, md), figs


def parse(path, source_dir):
    """
    解析一个 Markdown 题目文件。

    参数:
        path: 文件绝对路径
        source_dir: 来源目录
    返回:
        dict，含 problem_md、solution_md、figures；没有 Description 一节时返回 None
    """
    secs = sections(open(path, encoding="utf-8").read())
    names = [h.lower() for h, _ in secs]
    if "description" not in names:
        return None
    i = names.index("description")
    body = japanese_part(clean(secs[i][1]))
    problem_md, figs = localize(body, os.path.dirname(path), source_dir)
    sol = [f"### {h}\n\n{b}" for h, b in secs[i + 1:] if b and not h.lower().startswith(SOLUTION_SKIP)]
    solution_md, _ = localize(clean("\n\n".join(sol)), os.path.dirname(path), source_dir, placeholder=False)
    return {"problem_md": problem_md, "solution_md": solution_md, "figures": figs}


def split_title(title):
    """
    从“専門科目I 問題1”这类标题里分出题号和科目；找不到题号时整个标题当题号。

    参数:
        title: 去掉实施日期后的标题
    返回:
        (str 题号, str 科目)
    """
    tokens = title.split()
    for k in range(len(tokens) - 1, -1, -1):
        if LABEL.match(tokens[k]):
            return tokens[k], " ".join(tokens[:k] + tokens[k + 1:])
    return title or "1", ""


def page_record(label, section, parsed):
    """
    生成与转写结果同格式的单页记录。

    参数:
        label: 题号（显示用）
        section: 卷名/科目
        parsed: parse 的结果
    返回:
        dict
    """
    figs = [{"kind": "other", "bbox": [], "description": f["description"], "inner_text": "", "src": f["src"]}
            for f in parsed["figures"]]
    segs = [{"role": "problem", "problem_label": label, "continues_from_previous_page": False,
             "continues_to_next_page": False, "content_md": parsed["problem_md"], "figures": figs}]
    if parsed["solution_md"]:
        segs.append({"role": "solution", "problem_label": label, "continues_from_previous_page": False,
                     "continues_to_next_page": False, "content_md": parsed["solution_md"], "figures": []})
    result = {"page_type": "problem", "main_language": "ja", "has_handwriting": False, "header_text": "",
              "year_text": "", "section_title": section, "segments": segs, "corrections_note": "",
              "uncertain_spots": []}
    return {"prompt_version": "markdown", "effort": "", "image_size": [0, 0], "seconds": None, "via": "markdown",
            "result": result, "stop_reason": "", "error": "", "usage": {}, "cost": 0.0, "model": "",
            "page": 1, "src": ""}


def write_json(path, obj):
    """
    内容有变化时才写 JSON（保持缓存时间戳稳定）。

    参数:
        path: 文件路径
        obj: 对象
    返回:
        bool，是否写入
    """
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    if os.path.exists(path) and open(path, encoding="utf-8").read() == text:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def sync(pages_dir, struct_dir):
    """
    扫描全部 Markdown 来源，写出清单与页面、拼题缓存。

    参数:
        pages_dir: 页面缓存目录（data/full/pages）
        struct_dir: 拼题缓存目录（data/full/structure）
    返回:
        dict，含 docs、changed、skipped（没有题面的文件）
    """
    rows, changed, skipped = [], 0, []
    for src in sources():
        top = os.path.relpath(src, common.KAKOMON_DIR)
        for r in csv.DictReader(open(os.path.join(src, "index.csv"), encoding="utf-8-sig")):
            rel = f"{top}/{r['path']}"
            parsed = parse(os.path.join(src, r["path"]), src)
            if parsed is None or not parsed["problem_md"]:
                skipped.append(rel)
                continue
            did = doc_id(rel)
            title = r.get("paper_title", "")
            label, section = split_title(title)
            rec = page_record(label, section, parsed)
            changed += write_json(os.path.join(pages_dir, did, "pages.json"), {"total": 1, "content": [1]})
            changed += write_json(os.path.join(pages_dir, did, "p001.json"), rec)
            structure = {"problems": [{"label": label, "segment_ids": ["p001s0"],
                                       "solution_segment_ids": ["p001s1"] if parsed["solution_md"] else [],
                                       "title_hint": ""}],
                         "solutions": [], "errata": [], "translations": [], "unassigned": []}
            st = {"version": assemble.STRUCTURE_VERSION, "plan": [[1, 1]],
                  "chunks": {"0": {"range": [1, 1], "structure": structure, "usage": {}, "cost": 0.0,
                                   "via": "markdown"}}}
            changed += write_json(os.path.join(struct_dir, f"{did}.json"), st)
            rows.append({"doc_id": did, "rel_path": rel, "kind": "markdown", "university": r["university"],
                         "graduate_school": r["graduate_school"], "program": r["program"],
                         "program_folder": r["program"], "admission_year": r["admission_year"],
                         "exam_held": r.get("exam_held", ""), "document_kind": "single_problem",
                         "paper_title": title, "paper_category": "", "languages": "ja", "selection_rule": ""})
    with open(MANIFEST, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return {"docs": len(rows), "changed": changed, "skipped": skipped}


def copy_figure(src, dest):
    """
    把 Markdown 来源的插图存成 PNG（SVG 用 PyMuPDF 渲染）。

    参数:
        src: 相对项目根目录的原图路径
        dest: 输出 PNG 路径
    返回:
        bool，是否成功
    """
    import pymupdf
    from PIL import Image
    path = os.path.join(common.PROJECT_DIR, src)
    try:
        if path.lower().endswith(".svg"):
            with pymupdf.open(path) as doc:
                doc[0].get_pixmap(dpi=150, alpha=False).save(dest)
        else:
            Image.open(path).convert("RGB").save(dest)
        return True
    except Exception:
        return False
