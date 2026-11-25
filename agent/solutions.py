"""
解答存档：AI 解答和人工解答各存一个 Markdown 文件，文件名标明来源。

    data/solutions/<题目 ID>.ai.md       AI 解答（讲解后由 save_solution 保存，之后同一道题直接复用）
    data/solutions/<题目 ID>.human.md    人工解答（直接放文件即可，开头的元数据可省略）

文件开头可以有一段用 --- 包起来的元数据（covers 覆盖的小问、verified 核对情况、updated 更新时间），之后是正文。
取解答的优先顺序：题库收录的解答 > 人工解答 > AI 解答。目录可用环境变量 KAKOMON_SOLUTIONS 改（测试用）。
"""
import os
import re
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_DIR = os.path.join(DATA_DIR, "solutions")
LABELS = {"bank": "题库收录的解答", "human": "人工解答", "ai": "AI 解答（未经人工确认）"}
ORDER = ("bank", "human", "ai")


def folder():
    """
    解答目录。

    返回:
        str
    """
    return os.environ.get("KAKOMON_SOLUTIONS", DEFAULT_DIR)


def path_of(problem_id, kind):
    """
    某道题某种来源的解答文件路径。

    参数:
        problem_id: 题目 ID
        kind: ai 或 human
    返回:
        str
    """
    return os.path.join(folder(), f"{problem_id}.{kind}.md")


def read(problem_id, kind):
    """
    读取一份存档的解答。

    参数:
        problem_id: 题目 ID
        kind: ai 或 human
    返回:
        dict（kind、label、text、covers、verified、updated）；没有时返回 None
    """
    p = path_of(problem_id, kind)
    if not os.path.exists(p):
        return None
    raw = open(p, encoding="utf-8").read()
    meta, m = {}, re.match(r"---\n(.*?)\n---\n", raw, re.S)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
        raw = raw[m.end():]
    return {"kind": kind, "label": LABELS[kind], "text": raw.strip(), "covers": meta.get("covers", "全部"),
            "verified": meta.get("verified", ""), "updated": meta.get("updated", "")}


def save_ai(problem_id, text, covers=None, verified="", append=False):
    """
    保存 AI 解答。append 时接在已有解答后面（补讲其他小问，覆盖范围合并），否则整份替换（如修正错误后重写）。

    参数:
        problem_id: 题目 ID
        text: 解答正文（Markdown，公式用 LaTeX）
        covers: 讲了哪些小问（编号列表）；留空表示整道题
        verified: 核对情况（如“sympy 核对了特征值与 A=BDB*”），没核对留空
        append: 是否追加
    返回:
        dict，保存后的解答（同 read）
    """
    covers = [c for c in covers or [] if c]
    old = read(problem_id, "ai") if append else None
    if old:
        text = old["text"] + "\n\n" + text.strip()
        prev = [] if old["covers"] == "全部" else old["covers"].split("、")
        covers = [] if (not covers or not prev) else list(dict.fromkeys(prev + covers))
        verified = "；".join(x for x in (old["verified"], verified) if x)
    meta = {"kind": "ai", "problem_id": problem_id, "covers": "、".join(covers) or "全部",
            "verified": " ".join(verified.split()), "updated": datetime.now().strftime("%Y-%m-%d %H:%M")}
    os.makedirs(folder(), exist_ok=True)
    with open(path_of(problem_id, "ai"), "w", encoding="utf-8") as f:
        f.write("---\n" + "\n".join(f"{k}: {v}" for k, v in meta.items()) + "\n---\n\n" + text.strip() + "\n")
    return read(problem_id, "ai")


def available(problem_id, bank_solution=""):
    """
    这道题有哪些来源的解答。

    参数:
        problem_id: 题目 ID
        bank_solution: 题库里的解答正文
    返回:
        list[str]，按优先顺序，如 ["human", "ai"]
    """
    return [k for k in ORDER if (bank_solution if k == "bank" else os.path.exists(path_of(problem_id, k)))]


def best(problem_id, bank_solution="", bank_source=""):
    """
    按优先顺序取一份解答：题库收录 > 人工 > AI；题库解答来自 Markdown 社区来源时排在人工之后。

    参数:
        problem_id: 题目 ID
        bank_solution: 题库里的解答正文
        bank_source: 题库解答的来源文件
    返回:
        dict（同 read，题库解答另有 source）；都没有时返回 None
    """
    community = bank_source.endswith(".md")
    if bank_solution and not community:
        return {"kind": "bank", "label": LABELS["bank"], "text": bank_solution, "source": bank_source}
    human = read(problem_id, "human")
    if human or not bank_solution:
        return human or read(problem_id, "ai")
    return {"kind": "bank", "label": f"社区解答（{bank_source.split('/')[0]}，未经人工确认）",
            "text": bank_solution, "source": bank_source}
