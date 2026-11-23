"""
单页转写模块：把过去问页面图像转写成结构化的 Markdown + LaTeX（prompt v3）。

每页请求附带试卷清单中的文档上下文（文档类型、卷名、语言、说明）。
v3 新增页面级字段 year_text、section_title，供合订本按年度、科目拆题。
同步调用（transcribe_image）与 Batch（page_request）使用完全相同的请求。
"""
import json
import os
import time

import common

PROMPT_VERSION = "v3"
LONG_EDGE = 2000
MIN_INK = 0.012
MIN_TEXT_CHARS = 50
EFFORT = "low"

PAGE_TYPES = ["problem", "cover_instructions", "english_translation", "solution", "errata",
              "glossary", "answer_sheet", "blank_or_scratch", "other"]

FIGURE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["diagram", "circuit", "graph_plot", "table_image", "code_image", "other"]},
        "bbox": {"type": "array", "items": {"type": "integer"}},
        "description": {"type": "string"},
        "inner_text": {"type": "string"},
    },
    "required": ["kind", "bbox", "description", "inner_text"],
    "additionalProperties": False,
}

SEGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": ["problem", "solution", "other"]},
        "problem_label": {"type": "string"},
        "continues_from_previous_page": {"type": "boolean"},
        "continues_to_next_page": {"type": "boolean"},
        "content_md": {"type": "string"},
        "figures": {"type": "array", "items": FIGURE_SCHEMA},
    },
    "required": ["role", "problem_label", "continues_from_previous_page", "continues_to_next_page",
                 "content_md", "figures"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "page_type": {"type": "string", "enum": PAGE_TYPES},
        "main_language": {"type": "string", "enum": ["ja", "en", "ja_en", "none"]},
        "has_handwriting": {"type": "boolean"},
        "header_text": {"type": "string"},
        "year_text": {"type": "string"},
        "section_title": {"type": "string"},
        "segments": {"type": "array", "items": SEGMENT_SCHEMA},
        "corrections_note": {"type": "string"},
        "uncertain_spots": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["page_type", "main_language", "has_handwriting", "header_text", "year_text", "section_title",
                 "segments", "corrections_note", "uncertain_spots"],
    "additionalProperties": False,
}

SYSTEM = """你负责把日本大学院入学考试过去问的单页图像，逐字转写成可检索、可渲染的结构化文本。用户消息会给出这一页所在文档的上下文（文档类型、卷名、语言、说明），请结合上下文判断。

页面类型 page_type：
- problem 题目页；cover_instructions 封面/注意事项；english_translation 英译页；solution 解答；errata 订正；
  glossary 术语表；answer_sheet 答题纸；blank_or_scratch 空白/草稿页；other 其他。
- english_translation：本页只有英文，且所在文档有日文版题目（文档语言为 ja_en，或文件名含 je、说明提到日英对照/英文仅供参考），
  这时本页是日文题目的翻译，判为 english_translation，segments 返回空数组。
- 文档语言为 en（纯英文卷、没有日文版）时，英文题目页是原题，判为 problem 并照常转写。
- 只有 problem、solution、errata 页需要填写 segments；其他类型 segments 返回空数组。

页面级字段（所有页面类型都要填，包括封面）：
- header_text：页眉页脚中的年度、专攻、科目等文字原文（没有则留空）。
- year_text：本页印刷的年度或实施日期原文（标题、页眉、正文开头都算），如「平成26年度」「令和2年度4月期入学」「2019年8月実施」「AY 2014」；
  没有则留空。只照抄本页上印刷的文字，不要根据文件名或上下文推测。
- section_title：本页印刷的试卷或科目标题原文，如「数学」「英語」「専門科目I」「情報基礎」「一般教育科目 数学」；
  页中间出现新的科目标题时填新的标题；没有则留空。

segments 的 role：problem 题目原文；solution 解答、证明或解说；other 其他需要保留的正文（如订正说明）。
同一页里题目和解答都有时拆成两个 segments，problem_label 填同一个题号，role 分别为 problem 和 solution。

转写规则（content_md）：
1. 逐字照抄印刷体原文，不翻译、不改写、不补全、不解题。日文保留原样，包括全角数字与标点。
2. 数学一律用 LaTeX：行内 $...$，独立公式 $$...$$；矩阵用 \\begin{pmatrix}...\\end{pmatrix}，分段函数用 cases；上下标、积分上下限、向量粗体（\\mathbf）都要还原。
3. 公式里出现的日文（如「のとき」「は実数」「それ以外」）一律放进 \\text{...}，公式外的日文不要放进 $ 里。
4. 答案空栏：正文和公式中写成 $\\boxed{(1)}$（括号内照抄原编号，如 $\\boxed{\\text{ア}}$；无编号写 $\\boxed{\\phantom{00}}$）；代码块中写成 ［1］。
5. 保留题号与小问编号原样，如 (1)、(a)、問1、[A-1]，每个小问单独起一段。
6. 表格用 Markdown 表格；程序代码、伪代码放进 ``` 代码块并保留缩进与行号。
7. 同一页同时有日文和英文时，只转写日文部分。
8. 忽略所有手写笔迹（演算、答案、批注），只转写印刷体。
9. 红字或删除线等官方订正：按订正后的内容转写，并在 corrections_note 用中文说明改了什么。
10. 插图、电路图、自动机、图表等：在正文出现的位置写占位符 [图1]、[图2]……（有图号就用图号），并在 figures 中给出：
    - bbox=[x0,y0,x1,y1]：像素坐标，以给定图像左上角为原点，框住整幅图及图内标注；
    - description：一句中文，只说明图的类型和用途（如“7 个顶点的有向图”“台阶状势垒的示意图”），不要列举节点、边、数值等细节；
    - inner_text：图内印刷的文字、标签和公式，按从上到下、从左到右的顺序原样转写（公式用 LaTeX），用“；”分隔，没有则留空。
11. 一页里有多道大题时按大题拆成多个 segments；页首如果是上一页题目的延续，problem_label 留空并把 continues_from_previous_page 设为 true。
12. 看不清或拿不准的地方，在 uncertain_spots 用中文写明位置和原因，不要猜。"""


def manifest_index():
    """
    读取试卷清单原始抽取结果，按文档相对路径建立索引。

    返回:
        dict，键为文档 rel_path（照片为所在文件夹加“/”），值为模型抽取的元数据
    """
    path = os.path.join(common.DATA_DIR, "manifest_llm.jsonl")
    index = {}
    for line in open(path, encoding="utf-8"):
        rec = json.loads(line)
        index[rec["rel_path"]] = rec.get("llm") or {}
    return index


def context_text(meta):
    """
    把清单元数据整理成给模型的文档上下文。

    参数:
        meta: 清单中该文档的元数据
    返回:
        str
    """
    if not meta:
        return "文档上下文：无"
    notes = (meta.get("notes") or "")[:150]
    return (f"文档上下文：类型 {meta.get('document_kind', '')}；卷名 {meta.get('paper_title', '')}；"
            f"文档语言 {meta.get('languages', '')}；说明 {notes}")


def is_content_page(path, page_index):
    """
    判断页面是否有内容：文字层超过 MIN_TEXT_CHARS 个字符，或墨迹比例不低于 MIN_INK。
    “余白”页与空白页的墨迹比例约 0.006，会被排除。

    参数:
        path: PDF 或照片的绝对路径
        page_index: PDF 页码（照片忽略）
    返回:
        bool
    """
    if not path.lower().endswith(common.IMAGE_EXTS):
        text = common.open_pdf(path)[page_index].get_text().strip()
        if len(text) >= MIN_TEXT_CHARS:
            return True
    return common.content_ratio(common.page_image(path, page_index, 600)) >= MIN_INK


def page_request(img, rel_path, page_index, meta):
    """
    组装一页的转写请求参数。

    参数:
        img: PIL 页面图像（长边 LONG_EDGE）
        rel_path: 页面来源相对路径（用于提示）
        page_index: 页码（从 0 开始）
        meta: 该页所在文档的清单元数据
    返回:
        dict，common.request_params 的结果
    """
    text = (f"来源：{rel_path} 第 {page_index + 1} 页\n"
            f"{context_text(meta)}\n"
            f"图像尺寸：{img.width}x{img.height} 像素\n\n请转写这一页。")
    return common.request_params(SYSTEM, [common.image_block(img), {"type": "text", "text": text}],
                                 SCHEMA, effort=EFFORT, max_tokens=16000)


def page_record(res, img_size, seconds=None, via="sync"):
    """
    把一次转写的解析结果整理成页面缓存记录。

    参数:
        res: common.parse_message 的结果
        img_size: [宽, 高]
        seconds: 同步调用耗时（Batch 为 None）
        via: sync / batch / sync-retry
    返回:
        dict
    """
    return {"prompt_version": PROMPT_VERSION, "effort": EFFORT, "image_size": list(img_size), "seconds": seconds,
            "via": via, "result": res["data"], "stop_reason": res["stop_reason"], "error": res["error"],
            "usage": res["usage"], "cost": res["cost"], "model": res["model"]}


def transcribe_image(client, img, rel_path, page_index, meta):
    """
    同步转写一张页面图像。

    参数:
        client: anthropic 客户端
        img: PIL 页面图像（长边 LONG_EDGE）
        rel_path: 页面来源相对路径（用于提示）
        page_index: 页码（从 0 开始）
        meta: 该页所在文档的清单元数据
    返回:
        dict，见 page_record
    """
    start = time.time()
    res = common.call_params(client, page_request(img, rel_path, page_index, meta))
    return page_record(res, (img.width, img.height), round(time.time() - start, 1))
