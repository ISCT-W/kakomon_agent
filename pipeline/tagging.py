"""
打标签模块：按知识点表为每道大题的小问标注知识点、题型、难度、解法要点与小问间依赖。

知识点表放在系统提示中（会被缓存）；知识点 ID 与依赖的小问编号由代码校验，表外的知识点记入 missing_concepts。
同步调用（tag_problem）与 Batch（problem_request）使用完全相同的请求。
"""
import os
import sys

import yaml

import common

sys.path.insert(0, os.path.join(common.PROJECT_DIR, "agent"))
import labels  # noqa: E402

TAG_VERSION = "v2"
QUESTION_TYPES = ["calculation", "proof", "explanation", "programming", "code_reading", "design",
                  "fill_in", "choice", "other"]

TAG_SCHEMA = {
    "type": "object",
    "properties": {
        "title_zh": {"type": "string"},
        "subquestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "summary_zh": {"type": "string"},
                    "method_zh": {"type": "string"},
                    "primary_concept": {"type": "string"},
                    "secondary_concepts": {"type": "array", "items": {"type": "string"}},
                    "question_type": {"type": "string", "enum": QUESTION_TYPES},
                    "difficulty": {"type": "integer"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["label", "summary_zh", "method_zh", "primary_concept", "secondary_concepts",
                             "question_type", "difficulty", "depends_on"],
                "additionalProperties": False,
            },
        },
        "difficulty": {"type": "integer"},
        "est_minutes": {"type": "integer"},
        "missing_concepts": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["title_zh", "subquestions", "difficulty", "est_minutes", "missing_concepts", "notes"],
    "additionalProperties": False,
}

TAG_RULES = """你是日本大学院入学考试（情報系）过去问的出题分析专家。给你一道大题（大問）的原文，请为它的每个小问标注知识点、题型、难度、解法要点和依赖关系。

规则：
1. 按卷面最小的小问编号逐条列出（如 (1)(a)、(1)(b)、(2)），label 照抄卷面编号；大题没有小问时列一条，label 为空字符串。
2. primary_concept 必须是下方知识点表中的一个 ID（最能代表“做出这一问需要掌握的技能”的那一个）；secondary_concepts 最多 3 个，也必须是表中 ID。
3. 表中找不到合适的知识点时，primary_concept 填最接近的 ID，并把缺失的知识点用中文写进 missing_concepts。
4. summary_zh：一句中文说明这一问要求做什么（不超过 30 字），不要给出答案。
5. method_zh：一句中文写出这一问的解法要点或关键步骤（不超过 30 字，如“用极坐标换元把二重积分化为一元积分”），可以写方法，不要写出最终答案。
6. depends_on：这一问要用到本大题哪些小问的结论或结果，填那些小问的 label（照抄），没有则为空数组。
7. question_type：calculation 计算；proof 证明；explanation 说明论述；programming 编写程序；code_reading 读程序/追踪；design 设计（电路、算法、系统）；fill_in 填空；choice 选择；other 其他。
8. difficulty（小问与整题都要给，整数 1-5，以日本修士入试为基准）：
   1 直接套用定义或公式，一两步完成；
   2 标准题型，按固定步骤可解；
   3 需要组合两个以上知识点，或计算较长，或简单证明；
   4 需要非标准思路或构造，或较难的证明/设计；
   5 难题，需要多步构造与深入理解，容易出错。
9. est_minutes：熟练考生完成整道大题的预计分钟数。
10. title_zh：用中文概括整道大题考什么，不超过 30 字。
11. notes：需要提醒的情况（题目不完整、依赖插图、跨科目、附有勘误等），没有则留空。

知识点表（ID 名称）：
"""


def taxonomy_version(path):
    """
    知识点表的版本号（YAML 里的 version 字段）。

    参数:
        path: YAML 路径
    返回:
        str，如 v0
    """
    return str(yaml.safe_load(open(path, encoding="utf-8")).get("version", "v0"))


def load_taxonomy(path):
    """
    读取知识点表。

    参数:
        path: YAML 路径
    返回:
        (dict 知识点 ID -> 信息, str 供提示使用的文本)
    """
    data = yaml.safe_load(open(path, encoding="utf-8"))
    concepts, lines = {}, []
    for s in data["subjects"]:
        lines.append(f"{s['id']} {s['name']}（{s['ja']}）")
        for u in s["units"]:
            lines.append(f"  {u['id']} {u['name']}")
            for cid, name in u["concepts"].items():
                concepts[cid] = {"concept_id": cid, "name": name, "unit_id": u["id"], "unit": u["name"],
                                 "subject_id": s["id"], "subject": s["name"]}
                lines.append(f"    {cid} {name}")
    return concepts, "\n".join(lines)


def problem_text(problem, doc_row):
    """
    组装给模型的大题文本（含出处、插图说明、勘误与解答）。

    参数:
        problem: assemble.build_doc 产出（并经过挂接）的大题
        doc_row: 清单中的文档行（规范化后）
    返回:
        str
    """
    figs = "\n".join(f"[图{k}] {f['description']}；图内文字：{f.get('inner_text', '')}"
                     for k, f in enumerate(problem["figures"], 1))
    parts = [f"学校：{doc_row.get('university', '')} {doc_row.get('program', '')}；入学年度 {problem['admission_year']}；"
             f"卷名 {doc_row.get('paper_title', '')}；科目 {problem.get('section', '')}",
             f"大题编号：{problem['label']}（主题提示：{problem['title_hint']}）", "", "原文：", problem["content_md"]]
    if figs:
        parts += ["", "插图：", figs]
    if problem.get("errata_md"):
        parts += ["", "勘误：", problem["errata_md"][:2000]]
    if problem.get("solution_md"):
        parts += ["", "附带的解答（仅供判断难度和解法参考）：", problem["solution_md"][:3000]]
    return "\n".join(parts)


def problem_request(problem, doc_row, taxonomy_text):
    """
    组装一道大题的打标签请求参数。

    参数:
        problem: 大题
        doc_row: 清单中的文档行
        taxonomy_text: 知识点表文本
    返回:
        dict，common.request_params 的结果
    """
    return common.request_params(TAG_RULES + taxonomy_text, [{"type": "text", "text": problem_text(problem, doc_row)}],
                                 TAG_SCHEMA, effort="medium", max_tokens=16000)


def clamp(v, lo=1, hi=5):
    """
    把难度限制在 1-5。

    参数:
        v: 原始值
    返回:
        int，无法解析时为 0
    """
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return 0


def validate(tags, concepts):
    """
    校验并整理模型给出的标签：限定难度范围、剔除表外知识点、生成小问键、清理依赖。

    参数:
        tags: 模型输出（会被就地修改）
        concepts: 知识点字典
    返回:
        list[str]，被剔除的无效知识点 ID
    """
    invalid = []
    tags["difficulty"] = clamp(tags["difficulty"])
    keys = labels.unique_keys([labels.subq_key(sq["label"]) for sq in tags["subquestions"]])
    for sq, key in zip(tags["subquestions"], keys):
        sq["key"] = key
    known = set(keys)
    for sq in tags["subquestions"]:
        sq["difficulty"] = clamp(sq["difficulty"])
        if sq["primary_concept"] not in concepts:
            invalid.append(sq["primary_concept"])
        invalid += [c for c in sq["secondary_concepts"] if c not in concepts]
        sq["secondary_concepts"] = [c for c in sq["secondary_concepts"] if c in concepts][:3]
        sq["depends_on"] = [k for k in (labels.subq_key(d) for d in sq["depends_on"]) if k in known and k != sq["key"]]
    return invalid


def tag_problem(client, problem, doc_row, concepts, taxonomy_text):
    """
    同步为一道大题打标签并校验。

    参数:
        client: anthropic 客户端
        problem: 大题
        doc_row: 文档行
        concepts: 知识点字典
        taxonomy_text: 知识点表文本
    返回:
        dict，含 tags（校验后的结果）、invalid_ids、usage、cost、error
    """
    res = common.call_params(client, problem_request(problem, doc_row, taxonomy_text))
    tags = res["data"]
    invalid = validate(tags, concepts) if tags else []
    return {"tags": tags, "invalid_ids": invalid, "usage": res["usage"], "cost": res["cost"], "error": res["error"]}
