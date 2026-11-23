"""
题号与小问编号的规范化：生成稳定的题目键与小问键。

题目 ID、复习卡片、作答记录都用这里的键，避免同一编号的不同写法（「問題 I」「問題I」、「(1)(a)」「1(a)」）对不上。
数据管线（拼题、入库）与学习助手（记录作答）共用本模块。
"""
import re
import unicodedata

KANJI_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10,
         "XI": 11, "XII": 12}
PREFIX = re.compile(r"第|大問|大问|小問|小问|問題|问题|設問|设问|問|问|題|题|Problem|Question|Exercise|No\.?|Q\.", re.I)
YEAR_NOTE = re.compile(r"(?:昭和|平成|令和)\s*(?:元|\d{1,2})\s*年度?|(?:19|20)\d{2}\s*年度?|AY\s*(?:19|20)\d{2}", re.I)
PAREN_NOTE = re.compile(r"[(（][^()（）]*[)）]")
MAIN_NO = re.compile(r"(?:第|問題|大問|問)\s*([0-9]+|[〇零一二三四五六七八九十]+)\s*問?")


def nfkc(s):
    """
    Unicode 规范化（全角转半角、圈号数字转普通数字等）。

    参数:
        s: 字符串
    返回:
        str
    """
    return unicodedata.normalize("NFKC", s or "")


def kanji_number(s):
    """
    把字符串里的汉字数字（一至九十九）换成阿拉伯数字。

    参数:
        s: 字符串
    返回:
        str
    """
    def repl(m):
        t = m.group(0)
        if "十" not in t:
            return "".join(str(KANJI_DIGIT[c]) for c in t)
        tens, _, ones = t.partition("十")
        return str((KANJI_DIGIT.get(tens, 1) if tens else 1) * 10 + (KANJI_DIGIT.get(ones, 0) if ones else 0))
    return re.sub(r"[〇零一二三四五六七八九十]+", repl, s)


def problem_key(label):
    """
    大题编号的规范化键：去掉「第」「問題」「Problem」等前缀，汉字和罗马数字转阿拉伯数字，
    只保留字母、数字和数字之间的连字符。例：「問題 I」→ 1，「A-1」→ A1，「第3問」→ 3，「F1-1」→ F1-1。

    参数:
        label: 卷面题号原文
    返回:
        str，可能为空（如「プログラミング」）
    """
    s = YEAR_NOTE.sub("", PAREN_NOTE.sub("", nfkc(label)))
    s = kanji_number(PREFIX.sub("", s))
    s = re.sub(r"[\s\[\]【】()（）.。:：、,]", "", s).upper()
    s = re.sub(r"^(XII|XI|X|IX|VIII|VII|VI|V|IV|III|II|I)(?=$|-)", lambda m: str(ROMAN[m.group(1)]), s)
    s = re.sub(r"([A-Z])-(?=\d)", r"\1", s)
    s = re.sub(r"[^A-Z0-9-]", "", s).strip("-")
    return re.sub(r"-+", "-", s)


def main_number(label):
    """
    大题的主编号（跨文档挂接解答、勘误时用）：优先取「第N問」「問題N」「問N」中的 N（包括括号里的，
    如「Ⅲ (平成29年度第5問)」取 5）；否则取规范化题号的第一段（「A-1」取 A1，「3-2」取 3）。

    参数:
        label: 题号原文（可含小问、年度等附注）
    返回:
        str
    """
    m = MAIN_NO.search(nfkc(label))
    if m:
        return kanji_number(m.group(1))
    key = problem_key(label)
    return key.split("-")[0] if key else ""


def subq_key(label):
    """
    小问编号的规范化键：取出其中的数字、字母和汉字数字，小写后用连字符连接。
    例：「(1)(a)(i)」→ 1-a-i，「1(1)」→ 1-1，「問1」→ 1，「(3)-1」→ 3-1；空编号（无小问的大题）→ all。

    参数:
        label: 小问编号原文
    返回:
        str
    """
    s = kanji_number(PREFIX.sub("", nfkc(label)))
    tokens = re.findall(r"\d+|[A-Za-z]+", s)
    return "-".join(t.lower() for t in tokens) or "all"


def unique_keys(keys):
    """
    给重复的键加后缀 _2、_3……，保持原顺序（只用 Batch 请求 ID 允许的字符）。

    参数:
        keys: 键列表
    返回:
        list[str]
    """
    seen, out = {}, []
    for k in keys:
        seen[k] = seen.get(k, 0) + 1
        out.append(k if seen[k] == 1 else f"{k}_{seen[k]}")
    return out
