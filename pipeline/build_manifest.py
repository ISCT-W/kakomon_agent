"""
生成试卷清单：目录规则给出学校/研究科，Claude 读取每份文档前两张有内容的页面提取年度、卷别等信息。

每个 PDF 是一份文档；照片按所在文件夹合成一份文档。
支持断点续跑：已写入 manifest_llm.jsonl 的文档会跳过。

用法:
    python -I pipeline/build_manifest.py [--limit N] [--workers N]
输出:
    data/manifest_llm.jsonl  每份文档的原始抽取结果与用量
    data/manifest_raw.csv    原始抽取结果表（最终清单由 normalize_manifest.py 生成）
"""
import argparse
import csv
import hashlib
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

FOLDER_INFO = {
    "东北大": ("東北大学", "情報科学研究科", ""),
    "东大コンピュータ科学": ("東京大学", "情報理工学系研究科", "コンピュータ科学専攻"),
    "东大メディカル情報生命専攻 (CBMS)": ("東京大学", "新領域創成科学研究科", "メディカル情報生命専攻"),
    "东大創造情報学": ("東京大学", "情報理工学系研究科", "創造情報学専攻"),
    "东大工学数学过去问与解答": ("東京大学", "工学系研究科", "共通数学"),
    "东大広域システム科学系": ("東京大学", "総合文化研究科", "広域科学専攻 広域システム科学系"),
    "东大情理システム情報学": ("東京大学", "情報理工学系研究科", "システム情報学専攻"),
    "东大情理数学过去问（英文）": ("東京大学", "情報理工学系研究科", "共通数学"),
    "东大複雑理工学専攻": ("東京大学", "新領域創成科学研究科", "複雑理工学専攻"),
    "东大電子情報学": ("東京大学", "情報理工学系研究科", "電子情報学専攻"),
    "东大電気系工学専攻過去問": ("東京大学", "工学系研究科", "電気系工学専攻"),
    "东工システム制御系": ("東京工業大学", "工学院", "システム制御系"),
    "东工情报工学过去问": ("東京工業大学", "情報理工学院", "情報工学系"),
    "东工情通": ("東京工業大学", "工学院", "情報通信系"),
    "东工数理计算科学": ("東京工業大学", "情報理工学院", "数理・計算科学系"),
    "九州大学情報理工学専攻": ("九州大学", "システム情報科学府", "情報理工学専攻"),
    "京大先端数理": ("京都大学", "情報学研究科", "先端数理科学専攻"),
    "京大数学研究科": ("京都大学", "理学研究科", "数学・数理解析専攻"),
    "京大数理工学専攻": ("京都大学", "情報学研究科", "数理工学専攻"),
    "京大知能过去问": ("京都大学", "情報学研究科", "知能情報学専攻"),
    "京大系统过去问": ("京都大学", "情報学研究科", "システム科学専攻"),
    "京大通情": ("京都大学", "情報学研究科", "通信情報システム専攻"),
    "北海道大学情报学": ("北海道大学", "情報科学院", ""),
    "名大复杂科学": ("名古屋大学", "情報学研究科", "複雑系科学専攻"),
    "名大心理认知学": ("名古屋大学", "情報学研究科", "心理・認知科学専攻"),
    "名大数理情报": ("名古屋大学", "情報学研究科", "数理情報学専攻"),
    "名大知能": ("名古屋大学", "情報学研究科", "知能システム学専攻"),
    "名大社会情报": ("名古屋大学", "情報学研究科", "社会情報学専攻"),
    "大阪公立大学": ("大阪公立大学", "情報学研究科", ""),
    "大阪大学过去问": ("大阪大学", "情報科学研究科", ""),
    "庆应义塾过去问": ("慶應義塾大学", "理工学研究科", ""),
    "早稻田情报理工过去问": ("早稲田大学", "基幹理工学研究科", "情報理工・情報通信専攻"),
    "电通改版后过去问": ("電気通信大学", "情報理工学研究科", ""),
    "筑波大学CS过去问": ("筑波大学", "システム情報工学研究科", "コンピュータサイエンス専攻"),
}

DOC_KINDS = ["exam_paper", "single_problem", "compilation", "solutions", "errata",
             "glossary", "sample_problems", "other"]
PAPER_CATEGORIES = ["math", "specialized", "programming", "general", "mixed", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "document_kind": {"type": "string", "enum": DOC_KINDS},
        "university": {"type": "string"},
        "graduate_school": {"type": "string"},
        "program": {"type": "string"},
        "fiscal_year_text": {"type": "string"},
        "fiscal_year": {"type": "integer"},
        "exam_date_text": {"type": "string"},
        "exam_year": {"type": "integer"},
        "exam_month": {"type": "integer"},
        "year_source": {"type": "string", "enum": ["printed", "filename", "inferred", "unknown"]},
        "paper_title": {"type": "string"},
        "paper_category": {"type": "string", "enum": PAPER_CATEGORIES},
        "languages": {"type": "string", "enum": ["ja", "en", "ja_en"]},
        "problem_count_hint": {"type": "integer"},
        "selection_rule": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["document_kind", "university", "graduate_school", "program", "fiscal_year_text",
                 "fiscal_year", "exam_date_text", "exam_year", "exam_month", "year_source",
                 "paper_title", "paper_category", "languages", "problem_count_hint",
                 "selection_rule", "notes"],
    "additionalProperties": False,
}

SYSTEM = """你在为日本大学院（修士）入学考试过去问建立文档清单。你会看到一份文档的前一两张有内容的页面图像，以及它在资料库中的文件夹和文件名。

请按卷面印刷内容提取元数据，卷面没有写的才参考文件名，并在 year_source 中如实标注来源（printed / filename / inferred / unknown）。

文件名常见缩写：H=平成，R=令和，S=昭和；上传者用拼音缩写时 L 也指令和（Lìnghé），Qingbao=情報；gozen/gogo=午前/午後；kiso=基礎；senmon=専門；je=日英。只能依据文件名时，fiscal_year 填文件名中的年份，year_source 标 filename，并在 notes 说明无法确定它是入学年度还是实施年。

字段说明：
- document_kind：exam_paper 整份试卷或试卷的一个分册；single_problem 只含一道大题的文件；compilation 多年份或多份试卷的合订本；solutions 解答；errata 订正；glossary 术语对照表；sample_problems 样题/想定问题；other 其他。
- university / graduate_school / program：照卷面原文（日文）填写，没有写的留空字符串。
- fiscal_year_text：卷面上的“〇〇年度”原文（如“平成31年度”“2020年度”），没有则留空。
- fiscal_year：入学年度换算成西历整数（平成元年=1989，令和元年=2019），未知填 0。注意“2020年4月入学”属于 2020 年度。
- exam_date_text / exam_year / exam_month：实际考试日期原文与西历年、月，未知填 0。日本院试通常在入学前一年的 8 月或当年 1-2 月举行。
- paper_title：卷名原文，如“専門科目”“数学”“基礎科目A”“プログラミング”。
- paper_category：math 纯数学卷；specialized 专业科目；programming 编程/上机；general 一般教育或综合基础；mixed 数学与专业混合；other。
- languages：ja 只有日文；en 只有英文；ja_en 日英双语（同页或交替页都算）。
- problem_count_hint：卷面写明的大题总数（如“6問中3問選択”则填 6），看不出填 0。
- selection_rule：选做规则原文，如“6問のうち3問を選択”，没有留空。
- notes：用中文简要写出其他重要信息（例如合订本覆盖的年份范围、页面上有手写痕迹、红字订正等），没有留空。"""


def doc_id(rel_path):
    """
    由相对路径生成稳定的文档 ID。

    参数:
        rel_path: 相对 kakomon 目录的路径
    返回:
        str，形如 D1a2b3c4d
    """
    return "D" + hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:8]


def list_documents(root):
    """
    列出所有文档：每个 PDF 一份；同一文件夹内的照片合成一份。

    参数:
        root: kakomon 目录
    返回:
        list[dict]，含 rel_path、kind、files（页面来源列表）
    """
    docs = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        photos = sorted(f for f in filenames if f.lower().endswith(common.IMAGE_EXTS))
        for f in sorted(filenames):
            if f.lower().endswith(".pdf"):
                rel = os.path.relpath(os.path.join(dirpath, f), root)
                docs.append({"rel_path": rel, "kind": "pdf", "files": [os.path.join(dirpath, f)]})
        if photos:
            rel = os.path.relpath(dirpath, root)
            docs.append({"rel_path": rel + "/", "kind": "photos",
                         "files": [os.path.join(dirpath, f) for f in photos]})
    return docs


def doc_pages(doc):
    """
    返回文档所有页面的 (文件路径, 页码) 列表，以及有文字层的页数。

    参数:
        doc: list_documents 产出的字典
    返回:
        (list[(path, index)], int 文字层页数)
    """
    if doc["kind"] == "photos":
        return [(p, 0) for p in doc["files"]], 0
    pdf = common.open_pdf(doc["files"][0])
    text_pages = sum(1 for page in pdf if len(page.get_text().strip()) > 150)
    return [(doc["files"][0], i) for i in range(pdf.page_count)], text_pages


def first_content_pages(pages, n=2, long_edge=1400):
    """
    取前 n 张有内容的页面图像（跳过空白页和“余白”页）。

    参数:
        pages: (路径, 页码) 列表
        n: 需要的页数
        long_edge: 渲染长边像素
    返回:
        list[(页码序号, PIL 图像)]
    """
    out = []
    for i, (path, idx) in enumerate(pages):
        if len(out) >= n:
            break
        img = common.page_image(path, idx, long_edge)
        if common.content_ratio(img) >= 0.03:
            out.append((i, img))
    return out


def extract(client, doc):
    """
    对一份文档做规则字段统计并调用 Claude 抽取元数据。

    参数:
        client: anthropic 客户端
        doc: 文档字典
    返回:
        dict，可直接写入 jsonl
    """
    rel = doc["rel_path"]
    top = rel.split("/")[0]
    pages, text_pages = doc_pages(doc)
    sample = first_content_pages(pages)
    hint = f"文件夹：{os.path.dirname(rel.rstrip('/'))}\n文件名：{os.path.basename(rel.rstrip('/'))}\n总页数：{len(pages)}"
    content = [common.image_block(img) for _, img in sample]
    content.append({"type": "text", "text": hint + "\n\n请提取这份文档的元数据。"})
    result = common.call_json(client, SYSTEM, content, SCHEMA, effort="low", max_tokens=12000)
    uni, school, program = FOLDER_INFO.get(top, ("", "", ""))
    return {
        "doc_id": doc_id(rel), "rel_path": rel, "kind": doc["kind"], "pages": len(pages),
        "text_layer_pages": text_pages, "sampled_pages": [i for i, _ in sample],
        "folder_university": uni, "folder_school": school, "folder_program": program,
        "llm": result["data"], "stop_reason": result["stop_reason"], "error": result["error"],
        "usage": result["usage"], "cost": result["cost"], "model": result["model"],
    }


def write_csv(jsonl_path, csv_path):
    """
    把 jsonl 结果整理成 CSV 清单。

    参数:
        jsonl_path: manifest_llm.jsonl 路径
        csv_path: 输出 CSV 路径
    """
    rows = [json.loads(line) for line in open(jsonl_path, encoding="utf-8")]
    rows.sort(key=lambda r: r["rel_path"])
    fields = ["doc_id", "rel_path", "kind", "pages", "text_layer_pages",
              "folder_university", "folder_school", "folder_program"] + SCHEMA["required"] + ["error"]
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            row = {k: r.get(k, "") for k in fields[:8]}
            row.update(r.get("llm") or {})
            row["error"] = r.get("error", "")
            w.writerow(row)


def main():
    """
    解析参数，并发抽取尚未处理的文档，最后生成 CSV 并打印费用。
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--pick", default="", help="逗号分隔的路径片段，只处理路径包含其一的文档")
    args = ap.parse_args()
    os.makedirs(common.DATA_DIR, exist_ok=True)
    jsonl = os.path.join(common.DATA_DIR, "manifest_llm.jsonl")
    done = set()
    if os.path.exists(jsonl):
        ok = [line for line in open(jsonl, encoding="utf-8") if not json.loads(line).get("error")]
        with open(jsonl, "w", encoding="utf-8") as f:
            f.writelines(ok)
        done = {json.loads(line)["rel_path"] for line in ok}
    docs = [d for d in list_documents(common.KAKOMON_DIR) if d["rel_path"] not in done]
    if args.pick:
        keys = [k for k in args.pick.split(",") if k]
        docs = [d for d in docs if any(k in d["rel_path"] for k in keys)]
    if args.limit:
        docs = docs[: args.limit]
    client = common.make_client()
    lock = threading.Lock()
    total_cost, errors = 0.0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool, open(jsonl, "a", encoding="utf-8") as out:
        futures = {pool.submit(extract, client, d): d for d in docs}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                rec = fut.result()
            except Exception as e:
                rec = {"rel_path": futures[fut]["rel_path"], "error": f"{type(e).__name__}: {e}", "cost": 0.0}
                print("ERROR", rec["rel_path"], rec["error"], flush=True)
                errors += 1
                continue
            with lock:
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
            total_cost += rec["cost"]
            if rec["error"]:
                errors += 1
            if i % 25 == 0:
                print(f"{i}/{len(docs)} cost=${total_cost:.3f}", flush=True)
    write_csv(jsonl, os.path.join(common.DATA_DIR, "manifest_raw.csv"))
    print(f"processed={len(docs)} errors={errors} cost=${total_cost:.3f}")


if __name__ == "__main__":
    main()
