"""
题库查询（只读）：知识点表与别名、知识点搜索、候选小问（主/次知识点）、相邻知识点、分层找题、大题详情、题库概况。
"""
import json
import os
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict

import yaml

import labels
import solutions

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
DEFAULT_DB = os.path.join(DATA_DIR, "bank.sqlite")
DEFAULT_PAGES = os.path.join(DATA_DIR, "full", "pages")
ALIASES = os.path.join(DATA_DIR, "taxonomy", "aliases_v1.yaml")
TAXONOMY = os.path.join(DATA_DIR, "taxonomy", "taxonomy_v1.yaml")

UNIVERSITY_ALIASES = {
    "京大": "京都大学", "kyotouniversity": "京都大学", "kyodai": "京都大学",
    "东大": "東京大学", "東大": "東京大学", "东京大学": "東京大学", "universityoftokyo": "東京大学", "utokyo": "東京大学",
    "todai": "東京大学",
    "东工": "東京工業大学", "東工": "東京工業大学", "东工大": "東京工業大学", "東工大": "東京工業大学",
    "东京工业大学": "東京工業大学", "科学大": "東京工業大学", "東京科学大学": "東京工業大学", "东京科学大学": "東京工業大学",
    "sciencetokyo": "東京工業大学", "instituteofsciencetokyo": "東京工業大学", "tokyotech": "東京工業大学",
    "titech": "東京工業大学",
    "阪大": "大阪大学", "osakauniversity": "大阪大学", "handai": "大阪大学",
    "九大": "九州大学", "kyushuuniversity": "九州大学", "北大": "北海道大学", "hokkaidouniversity": "北海道大学",
    "名大": "名古屋大学", "nagoyauniversity": "名古屋大学", "东北大": "東北大学", "東北大": "東北大学",
    "东北大学": "東北大学", "tohokuuniversity": "東北大学", "筑波": "筑波大学", "universityoftsukuba": "筑波大学",
    "早稻田": "早稲田大学", "早大": "早稲田大学", "waseda": "早稲田大学", "庆应": "慶應義塾大学", "慶應": "慶應義塾大学",
    "keio": "慶應義塾大学", "电通": "電気通信大学", "電通": "電気通信大学", "电通大": "電気通信大学",
    "電通大": "電気通信大学", "uec": "電気通信大学", "阪公大": "大阪公立大学", "大阪公立": "大阪公立大学",
    "osakametropolitan": "大阪公立大学", "东科大": "東京工業大学", "東科大": "東京工業大学", "东科": "東京工業大学",
    "東科": "東京工業大学",
}

SEVEN_IMPERIAL = ["東京大学", "京都大学", "大阪大学", "東北大学", "名古屋大学", "九州大学", "北海道大学"]
UNIVERSITY_GROUPS = {"七帝大": SEVEN_IMPERIAL, "旧帝大": SEVEN_IMPERIAL, "旧七帝大": SEVEN_IMPERIAL,
                     "七大": SEVEN_IMPERIAL, "帝大": SEVEN_IMPERIAL}
UNIVERSITY_SEP = re.compile(r"[,，、+＋/／;；]+")
MATH = ["la", "calc", "anal", "vec", "ode", "cplx", "trans", "prob", "stat", "disc", "logic", "num", "opt"]
CS = ["algo", "prog", "arch", "os", "net", "db", "theory", "lc", "info", "ml", "media"]
SUBJECT_GROUPS = {"数学": MATH, "math": MATH, "计算机": CS, "情报": CS, "情報": CS, "cs": CS}

PROGRAM_ALIASES = {
    "计算机": ["コンピュータ", "計算機", "情報工学", "Computer"], "cs": ["コンピュータ", "Computer"],
    "computerscience": ["コンピュータ", "Computer"], "computer": ["コンピュータ", "Computer"], "信息": ["情報"], "情报": ["情報"], "informatics": ["情報"], "information": ["情報"],
    "智能": ["知能"], "intelligence": ["知能"], "系统": ["システム"], "system": ["システム"], "网络": ["ネットワーク"],
    "network": ["ネットワーク"], "communication": ["通信"], "数学": ["数理", "数学"], "math": ["数理", "数学"],
    "电气": ["電気"], "电子": ["電子"], "electrical": ["電気"], "electronic": ["電子"], "医学": ["メディカル"],
    "medical": ["メディカル"], "生命": ["生命"], "生物": ["生命"], "复杂": ["複雑"], "complex": ["複雑"],
    "创造": ["創造"], "creative": ["創造"], "心理": ["心理"], "认知": ["認知"], "媒体": ["メディア"],
    "media": ["メディア"], "控制": ["制御"], "control": ["制御"], "社会": ["社会"],
}


def norm(s):
    """
    匹配用的规范化：全半角统一、小写、去掉所有空白。

    参数:
        s: 字符串
    返回:
        str
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).lower()


def load_aliases(path=ALIASES):
    """
    读取知识点别名表。

    参数:
        path: YAML 路径
    返回:
        dict，知识点 ID -> 别名列表
    """
    if not os.path.exists(path):
        return {}
    data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    return {k: [str(x) for x in v] for k, v in data.items()}


ALIAS_MAP = load_aliases()


def load_subject_ja(path=TAXONOMY):
    """
    读取科目的日文名。

    参数:
        path: 知识点表路径
    返回:
        dict，科目 ID -> 日文名
    """
    if not os.path.exists(path):
        return {}
    data = yaml.safe_load(open(path, encoding="utf-8"))
    return {s["id"]: s.get("ja", "") for s in data["subjects"]}


SUBJECT_JA = load_subject_ja()


def connect(path=None):
    """
    打开题库数据库（只读用途）。

    参数:
        path: 数据库路径，默认环境变量 KAKOMON_DB 或 data/bank.sqlite
    返回:
        sqlite3.Connection（行可按列名访问）
    """
    con = sqlite3.connect(path or os.environ.get("KAKOMON_DB", DEFAULT_DB), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def resolve_university(name):
    """
    把学校简称、英文名、新校名映射为题库中的校名。

    参数:
        name: 用户输入的学校名
    返回:
        str，题库中的校名（无法识别时原样返回）
    """
    key = (name or "").strip()
    return UNIVERSITY_ALIASES.get(key) or UNIVERSITY_ALIASES.get(norm(key)) or key


def resolve_universities(names):
    """
    把一所或多所学校的写法展开成题库中的校名列表：可以用逗号、顿号、加号、斜杠分隔多所学校，
    也可以写学校组（“七帝大”“旧帝大”展开成 7 所旧帝国大学）。例：“七帝大+东科大” -> 8 所。

    参数:
        names: 字符串或字符串列表
    返回:
        list[str]，去重后的校名
    """
    parts = names if isinstance(names, (list, tuple)) else UNIVERSITY_SEP.split(names or "")
    out = []
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        for name in UNIVERSITY_GROUPS.get(p) or UNIVERSITY_GROUPS.get(norm(p)) or [resolve_university(p)]:
            if name not in out:
                out.append(name)
    return out


def resolve_program(keyword):
    """
    把专攻关键词（中文、英文、日文）展开成用于匹配题库专攻名的日文片段。

    参数:
        keyword: 用户输入的专攻关键词
    返回:
        list[str]
    """
    k = norm(keyword)
    out = [keyword.strip()] if keyword.strip() else []
    for alias, parts in PROGRAM_ALIASES.items():
        if alias in k:
            out += [p for p in parts if p not in out]
    return out


def program_match(text, parts):
    """
    专攻名是否命中关键词片段（ASCII 不区分大小写）。

    参数:
        text: 专攻名（清单专攻名与目录专攻名拼在一起）
        parts: resolve_program 的结果
    返回:
        bool
    """
    t = (text or "").lower()
    return any(x.lower() in t for x in parts)


def resolve_subjects(con, names):
    """
    把科目的写法（科目 ID、中文名、科目组“数学”“计算机”）展开成科目 ID 列表。

    参数:
        con: 数据库连接
        names: 字符串或字符串列表，如 ["数学"]、["la", "概率论"]
    返回:
        list[str]；对不上的写法原样保留（当作 ID 前缀）
    """
    names = [names] if isinstance(names, str) else list(names or [])
    by_name = {norm(r["subject"]): r["subject_id"] for r in con.execute("SELECT DISTINCT subject_id, subject FROM concepts")}
    out = []
    for n in names:
        for sid in SUBJECT_GROUPS.get(n) or SUBJECT_GROUPS.get(norm(n)) or [by_name.get(norm(n), n)]:
            if sid and sid not in out:
                out.append(sid)
    return out


def in_subjects(concept_id, subject_ids):
    """
    知识点是否属于给定科目（科目 ID 为空表示不限）。

    参数:
        concept_id: 知识点 ID
        subject_ids: resolve_subjects 的结果
    返回:
        bool
    """
    return not subject_ids or any(concept_id == s or concept_id.startswith(s + ".") for s in subject_ids)


def exam_trends(con, university="", program="", subjects=(), top_n=15):
    """
    出题趋势：某校（某专攻、某些科目）过去问里各知识点（主知识点）考过的年度和大题数，按考过的年数排序。

    参数:
        con: 数据库连接
        university: 学校（可用简称、学校组），留空为全部学校
        program: 专攻关键词，留空不限
        subjects: 科目（ID、中文名或科目组“数学”），留空不限
        top_n: 返回前几个知识点
    返回:
        dict：scope（学校、匹配到的专攻、年度、大题数）与 concepts（name、unit、problems、years、last）
    """
    unis = resolve_universities(university) if university else []
    parts = resolve_program(program) if program else []
    sids = resolve_subjects(con, subjects)
    rows = [r for r in con.execute("""
        SELECT DISTINCT sc.concept_id, c.name, c.unit, p.problem_id, p.admission_year AS year, d.university,
               d.program, COALESCE(d.program_folder, '') AS folder
        FROM subq_concepts sc JOIN concepts c ON c.concept_id = sc.concept_id
        JOIN subquestions sq ON sq.subq_id = sc.subq_id JOIN problems p ON p.problem_id = sq.problem_id
        JOIN documents d ON d.doc_id = p.doc_id
        WHERE sc.role = 'primary' AND (p.duplicate_of IS NULL OR p.duplicate_of = '')""")
            if (not unis or any(u in r["university"] for u in unis)) and in_subjects(r["concept_id"], sids)
            and (not parts or program_match(f"{r['program']} {r['folder']}", parts))]
    by_c = defaultdict(lambda: {"problems": set(), "years": set()})
    for r in rows:
        c = by_c[r["concept_id"]]
        c.update(name=r["name"], unit=r["unit"])
        c["problems"].add(r["problem_id"])
        if r["year"]:
            c["years"].add(r["year"])
    ranked = sorted(by_c.items(), key=lambda kv: (-len(kv[1]["years"]), -max(kv[1]["years"] or {0}), -len(kv[1]["problems"])))
    return {"scope": {"universities": sorted({r["university"] for r in rows}),
                      "programs": sorted({r["folder"] or r["program"] for r in rows}),
                      "years": year_ranges({r["year"] for r in rows if r["year"]}), "problems": len({r["problem_id"] for r in rows})},
            "concepts": [{"concept_id": cid, "name": c["name"], "unit": c["unit"], "problems": len(c["problems"]),
                          "years": year_ranges(c["years"]), "last": max(c["years"] or {0}) or None}
                         for cid, c in ranked[:top_n]]}


def matched_programs(con, keyword, university=""):
    """
    专攻关键词实际匹配到的题库专攻（返回给 Claude 向学生确认）；给了学校时只看这所学校。

    参数:
        con: 数据库连接
        keyword: 专攻关键词
        university: 学校（可选）
    返回:
        list[str]，“学校 专攻”
    """
    parts, unis = resolve_program(keyword), resolve_universities(university) if university else []
    out = set()
    for r in con.execute("SELECT DISTINCT university, program, program_folder FROM documents"):
        if (not unis or any(u in r["university"] for u in unis)) and \
                program_match(f"{r['program']} {r['program_folder']}", parts):
            out.add(f"{r['university']} {r['program_folder'] or r['program']}")
    return sorted(out)


def taxonomy(con, subject_id=""):
    """
    返回知识点表及每个知识点作为主知识点的小问数。

    参数:
        con: 数据库连接
        subject_id: 只看某个科目（可选）
    返回:
        list[dict]，科目 -> 单元 -> 知识点 的嵌套结构
    """
    rows = con.execute("""
        SELECT c.subject_id, c.subject, c.unit_id, c.unit, c.concept_id, c.name,
               COUNT(p.problem_id) AS n
        FROM concepts c LEFT JOIN subq_concepts sc ON sc.concept_id = c.concept_id AND sc.role = 'primary'
        LEFT JOIN subquestions sq ON sq.subq_id = sc.subq_id
        LEFT JOIN problems p ON p.problem_id = sq.problem_id AND (p.duplicate_of IS NULL OR p.duplicate_of = '')
        WHERE (? = '' OR c.subject_id = ?)
        GROUP BY c.concept_id ORDER BY c.concept_id""", (subject_id, subject_id)).fetchall()
    subjects = {}
    for r in rows:
        s = subjects.setdefault(r["subject_id"], {"subject_id": r["subject_id"], "subject": r["subject"], "n": 0, "units": {}})
        u = s["units"].setdefault(r["unit_id"], {"unit_id": r["unit_id"], "unit": r["unit"], "n": 0, "concepts": []})
        u["concepts"].append({"concept_id": r["concept_id"], "name": r["name"], "n": r["n"]})
        u["n"] += r["n"]
        s["n"] += r["n"]
    return [dict(s, units=list(s["units"].values())) for s in subjects.values()]


def taxonomy_compact(con, subject_id=""):
    """
    紧凑文本形式的知识点表（每行一个知识点：ID、名称、别名、题量），供 Claude 一次读完后做语义对应。

    参数:
        con: 数据库连接
        subject_id: 只看某个科目（留空为全部，约 2.7 万字）
    返回:
        str
    """
    lines = ["# 知识点 ID 名称 | 别名（日文/英文/中文同义词） | n=主知识点小问数"]
    for s in taxonomy(con, subject_id):
        lines.append(f"{s['subject_id']} {s['subject']}（{SUBJECT_JA.get(s['subject_id'], '')}） n={s['n']}")
        for u in s["units"]:
            lines.append(f" {u['unit_id']} {u['unit']} n={u['n']}")
            for c in u["concepts"]:
                alias = "、".join(ALIAS_MAP.get(c["concept_id"], []))
                lines.append(f"  {c['concept_id']} {c['name']} | {alias} | n={c['n']}")
    return "\n".join(lines)


def search_concepts(con, keyword):
    """
    在知识点、单元、科目的名称、ID 与别名中查找（统一全半角、忽略空白和大小写）。

    参数:
        con: 数据库连接
        keyword: 关键词（中文、日文、英文或 ID 片段）
    返回:
        list[dict]，含 concept_id、subject、unit、name、n（题量）
    """
    kw = norm(keyword)
    if not kw:
        return []
    rows = con.execute("""
        SELECT c.*, COUNT(p.problem_id) AS n FROM concepts c
        LEFT JOIN subq_concepts sc ON sc.concept_id = c.concept_id AND sc.role = 'primary'
        LEFT JOIN subquestions sq ON sq.subq_id = sc.subq_id
        LEFT JOIN problems p ON p.problem_id = sq.problem_id AND (p.duplicate_of IS NULL OR p.duplicate_of = '')
        GROUP BY c.concept_id ORDER BY n DESC, c.concept_id""").fetchall()
    out = []
    for r in rows:
        hay = norm(" ".join([r["concept_id"], r["name"], r["unit"], r["subject"]] + ALIAS_MAP.get(r["concept_id"], [])))
        if kw in hay:
            out.append({"concept_id": r["concept_id"], "subject": r["subject"], "unit": r["unit"],
                        "name": r["name"], "n": r["n"]})
    return out


def concept_ids(con, prefixes):
    """
    把知识点 ID 或前缀展开成具体的知识点 ID。

    参数:
        con: 数据库连接
        prefixes: 知识点 ID 或前缀列表
    返回:
        set[str]
    """
    return {r[0] for r in con.execute("SELECT concept_id FROM concepts")
            if any(r[0] == p or r[0].startswith(p + ".") or r[0].startswith(p) for p in prefixes)}


def candidates(con, concept_prefixes, university="", program="", roles=("primary",)):
    """
    按知识点（ID 或前缀）与学校、专攻筛选候选小问。

    参数:
        con: 数据库连接
        concept_prefixes: 知识点 ID 或前缀列表，如 ["la.eigen"]
        university: 学校（可用简称、英文名）；多所学校用逗号或加号连接，也可写学校组，如“七帝大+东科大”
        program: 专攻关键词（中文、英文、日文均可）
        roles: 匹配哪些角色的知识点：primary 主知识点 / secondary 次知识点
    返回:
        list[sqlite3.Row]
    """
    if not concept_prefixes:
        return []
    cond = " OR ".join("sc.concept_id LIKE ?" for _ in concept_prefixes)
    params = [p + "%" for p in concept_prefixes]
    sql = f"""
        SELECT sq.subq_id, sq.label AS subq_label, sq.skey AS subq_key, sq.summary_zh, sq.difficulty, sq.question_type,
               sq.seq AS subq_seq, sq.depends_on, sc.concept_id, sc.role, p.problem_id, p.label AS problem_label,
               p.title_zh, p.difficulty AS problem_difficulty, p.est_minutes, d.university, d.program,
               p.admission_year
        FROM subq_concepts sc JOIN subquestions sq ON sq.subq_id = sc.subq_id
        JOIN problems p ON p.problem_id = sq.problem_id JOIN documents d ON d.doc_id = p.doc_id
        WHERE sc.role IN ({",".join("?" * len(roles))}) AND ({cond}) AND (p.duplicate_of IS NULL OR p.duplicate_of = '')"""
    params = list(roles) + params
    if university:
        unis = resolve_universities(university)
        sql += " AND (" + " OR ".join("d.university LIKE ?" for _ in unis) + ")"
        params += ["%" + u + "%" for u in unis]
    if program:
        parts = resolve_program(program)
        sql += " AND (" + " OR ".join("(d.program LIKE ? OR d.program_folder LIKE ?)" for _ in parts) + ")"
        params += ["%" + x + "%" for x in parts for _ in (0, 1)]
    return con.execute(sql + " ORDER BY sq.difficulty, p.admission_year", params).fetchall()


def neighbors(con, concept_prefixes, limit=6):
    """
    相邻知识点：同一单元的其他知识点，以及在题库里经常和所选知识点出现在同一小问的知识点。
    按共同出现次数排序（同单元加 0.5），只保留题库里有主知识点小问的。

    参数:
        con: 数据库连接
        concept_prefixes: 所选知识点 ID 或前缀
        limit: 最多返回几个
    返回:
        list[str]，知识点 ID
    """
    chosen = concept_ids(con, concept_prefixes)
    if not chosen:
        return []
    units = {r[0] for r in con.execute(f"SELECT DISTINCT unit_id FROM concepts WHERE concept_id IN "
                                       f"({','.join('?' * len(chosen))})", list(chosen))}
    score = Counter()
    for r in con.execute(f"""
            SELECT b.concept_id, COUNT(*) FROM subq_concepts a JOIN subq_concepts b ON a.subq_id = b.subq_id
            WHERE a.concept_id IN ({','.join('?' * len(chosen))}) AND b.concept_id != a.concept_id
            GROUP BY b.concept_id""", list(chosen)):
        score[r[0]] += r[1]
    for r in con.execute(f"SELECT concept_id FROM concepts WHERE unit_id IN ({','.join('?' * len(units))})", list(units)):
        score[r[0]] += 0.5
    available = {r[0] for r in con.execute(f"""
        SELECT DISTINCT sc.concept_id FROM subq_concepts sc JOIN subquestions sq ON sq.subq_id = sc.subq_id
        JOIN problems p ON p.problem_id = sq.problem_id WHERE sc.role = 'primary' AND (p.duplicate_of IS NULL OR p.duplicate_of = '')""")}
    ranked = [c for c, _ in score.most_common() if c not in chosen and c in available]
    return ranked[:limit]


def term_variants(term):
    """
    检索词的写法变体：原样、NFKC 半角、ASCII 转全角（卷面数字和字母常为全角）。

    参数:
        term: 检索词
    返回:
        list[str]
    """
    half = unicodedata.normalize("NFKC", term)
    full = "".join(chr(ord(ch) + 0xFEE0) if 0x21 <= ord(ch) <= 0x7E else ch for ch in half)
    return list(dict.fromkeys([term, half, full]))


def snippet(text, variants, before=40, after=80):
    """
    截取文本中第一个命中位置附近的片段（换行压成空格）。

    参数:
        text: 原文
        variants: 检索词变体
        before: 命中位置前保留的字符数
        after: 命中位置后保留的字符数
    返回:
        str；没有命中时返回空字符串
    """
    low = (text or "").lower()
    hits = [i for i in (low.find(v.lower()) for v in variants) if i >= 0]
    if not hits:
        return ""
    i = min(hits)
    start, end = max(0, i - before), min(len(text), i + after)
    s = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + s + ("…" if end < len(text) else "")


def keyword_hits(con, keyword):
    """
    用全文索引查一个关键词命中的大题（原文、中文标题、小问摘要与解法要点）。

    参数:
        con: 数据库连接
        keyword: 关键词
    返回:
        dict，题目 ID -> 原文片段（命中在摘要里时为空字符串）
    """
    vs = term_variants(keyword.strip())
    if not vs or not vs[0]:
        return {}
    if all(len(v) >= 3 for v in vs):
        q = " OR ".join('"' + v.replace('"', '""') + '"' for v in vs)
        rows = con.execute("SELECT problem_id, content_md FROM problems_fts WHERE problems_fts MATCH ?", (q,)).fetchall()
    else:
        cond = " OR ".join("title_zh LIKE ? OR content_md LIKE ? OR summaries LIKE ?" for _ in vs)
        rows = con.execute(f"SELECT problem_id, content_md FROM problems_fts WHERE {cond}",
                           [f"%{v}%" for v in vs for _ in range(3)]).fetchall()
    return {r["problem_id"]: snippet(r["content_md"], vs) for r in rows}


SUBQ_SHOWN = 10


def brief_subq(x, width=60):
    """
    把一个小问压缩成一行文本（找题结果用，省篇幅）。

    参数:
        x: 小问行（含 label、question_type、difficulty、concepts、summary_zh、method_zh）
        width: 摘要和解法要点各自最多保留的字数
    返回:
        str，如 "(1)｜calculation｜难2｜la.eigen.compute｜求矩阵的特征值｜解法：特征多项式因式分解"
    """
    cut = lambda t: (t or "")[:width] + ("…" if len(t or "") > width else "")  # noqa: E731
    return "｜".join([x["label"] or "（整题）", x["question_type"] or "", f"难{x['difficulty'] or '?'}",
                     x["concepts"] or "", cut(x["summary_zh"]), "解法：" + cut(x["method_zh"])])


def find_problems(con, concepts=(), keywords=(), university="", program="", question_type="",
                  difficulty_min=0, difficulty_max=0, limit=20, brief=False):
    """
    分层找题的检索层：按知识点和关键词召回候选大题，再按学校、专攻、题型、难度过滤，返回紧凑信息供 Claude 阅读挑选。
    得分：给出的每个知识点（ID 或前缀）命中主知识点得 2 分、只命中次知识点得 1 分，各知识点相加
    （找相似题时传入原题的几个知识点，共同知识点越多越靠前）；每个关键词命中再加 1.5 分。
    每个小问压缩成一行“编号｜题型｜难度｜主知识点｜摘要｜解法要点”，一道题最多列 SUBQ_SHOWN 个小问。

    参数:
        con: 数据库连接
        concepts: 知识点 ID 或前缀
        keywords: 关键词（日文术语召回原文，中文词召回中文标题与小问摘要）
        university: 学校
        program: 专攻关键词
        question_type: 只要含这种题型小问的题（calculation、proof、programming 等）
        difficulty_min: 小问难度下限（0 不限）
        difficulty_max: 小问难度上限（0 不限）
        limit: 最多返回几道
        brief: 只返回出处、标题和难度（不列小问），候选多时先粗看
    返回:
        (list[dict] 候选大题, int 过滤后的候选总数)
    """
    score, why, snippets = defaultdict(float), defaultdict(list), {}
    items = list(concepts or [])
    hits = defaultdict(dict)
    for r in candidates(con, items, roles=("primary", "secondary")) if items else []:
        s = 2.0 if r["role"] == "primary" else 1.0
        for q in items:
            if r["concept_id"].startswith(q) and s > hits[r["problem_id"]].get(q, 0):
                hits[r["problem_id"]][q] = s
    for pid, h in hits.items():
        score[("c", pid)] = sum(h.values())
    for kw in keywords or []:
        for pid, snip in keyword_hits(con, kw).items():
            score[("k" + kw, pid)] = 1.5
            if snip and pid not in snippets:
                snippets[pid] = snip
    total = defaultdict(float)
    for (kind, pid), s in score.items():
        total[pid] += s
        if kind == "c":
            why[pid].append("知识点：" + "、".join(hits[pid]) if len(items) > 1 else "知识点")
        else:
            why[pid].append("关键词：" + kind[1:])
    return rank(con, total, why, snippets, university, program, question_type, difficulty_min, difficulty_max, limit, brief)


def rank(con, total, why, snippets=None, university="", program="", question_type="", difficulty_min=0,
         difficulty_max=0, limit=20, brief=False):
    """
    把打好分的候选大题过滤（学校、专攻、题型、难度，跳过重复题）、排序（得分高、难度低的在前），整理成紧凑结果。

    参数:
        con: 数据库连接
        total: dict，题目 ID -> 得分
        why: dict，题目 ID -> 命中原因列表
        snippets: dict，题目 ID -> 原文片段
        university / program / question_type / difficulty_min / difficulty_max / limit / brief: 同 find_problems
    返回:
        (list[dict] 候选大题, int 过滤后的候选总数)
    """
    if not total:
        return [], 0
    snippets = snippets or {}
    pids = list(total)
    rows = {r["problem_id"]: r for r in con.execute(f"""
        SELECT p.problem_id, p.label, p.section, p.admission_year, p.title_zh, p.difficulty, p.est_minutes,
               p.solution_md != '' AS has_solution, d.university, d.program, d.program_folder, d.paper_title
        FROM problems p JOIN documents d ON d.doc_id = p.doc_id
        WHERE p.problem_id IN ({','.join('?' * len(pids))}) AND (p.duplicate_of IS NULL OR p.duplicate_of = '')""", pids)}
    seen_in = also_in(con, list(rows))
    unis = resolve_universities(university) if university else []
    parts = resolve_program(program) if program else []
    subqs = defaultdict(list)
    for s in con.execute(f"""
            SELECT sq.problem_id, sq.label, sq.summary_zh, sq.method_zh, sq.question_type, sq.difficulty,
                   GROUP_CONCAT(CASE WHEN sc.role = 'primary' THEN sc.concept_id END) AS concepts
            FROM subquestions sq LEFT JOIN subq_concepts sc ON sc.subq_id = sq.subq_id
            WHERE sq.problem_id IN ({','.join('?' * len(pids))}) GROUP BY sq.subq_id ORDER BY sq.seq""", pids):
        subqs[s["problem_id"]].append(dict(s))
    out = []
    for pid in pids:
        r = rows.get(pid)
        if not r or (unis and not any(u in r["university"] for u in unis)) or \
                (parts and not program_match(f"{r['program']} {r['program_folder']}", parts)):
            continue
        sq = subqs[pid]
        if question_type and not any(x["question_type"] == question_type for x in sq):
            continue
        if difficulty_min and not any((x["difficulty"] or 0) >= difficulty_min for x in sq):
            continue
        if difficulty_max and not any(0 < (x["difficulty"] or 0) <= difficulty_max for x in sq):
            continue
        row = {"problem_id": pid, "score": round(total[pid], 1), "matched_by": why[pid],
               "source": {"university": r["university"], "program": r["program"], "admission_year": r["admission_year"],
                          "paper_title": r["paper_title"], "problem_label": r["label"], "section": r["section"]},
               "title_zh": r["title_zh"], "difficulty": r["difficulty"], "est_minutes": r["est_minutes"],
               "solutions": solutions.available(pid, r["has_solution"]), "also_in": seen_in.get(pid, [])}
        if not brief:
            row["snippet"] = snippets.get(pid, "")
            row["subquestions"] = [brief_subq(x) for x in sq[:SUBQ_SHOWN]] + \
                ([f"……另有 {len(sq) - SUBQ_SHOWN} 个小问"] if len(sq) > SUBQ_SHOWN else [])
        out.append(row)
    out.sort(key=lambda x: (-x["score"], x["difficulty"] or 3))
    return out[:limit], len(out)


def similar_problems(con, problem_id, subq_labels=(), university="", limit=8):
    """
    以小问为单位找相似题：取原题（或指定小问）的知识点，找共享这些知识点的其他大题。
    得分：每个共同知识点按两边的角色相乘（主 2、次 1：主×主 4、主×次 2、次×次 1）后相加；有同题型的小问再加 1；
    整题难度每差 1 扣 0.5。原题本身排除，重复题本来就不参与。

    参数:
        con: 数据库连接
        problem_id: 原题 ID
        subq_labels: 只按这些小问找（任意写法），留空按整道题
        university: 只在这些学校里找（可用简称、学校组），留空不限
        limit: 最多返回几道
    返回:
        (list[dict] 候选大题（格式同 find_problems）, int 候选总数)；原题不存在时返回 None
    """
    detail = problem_detail(con, problem_id)
    if not detail:
        return None
    keys = resolve_subqs(detail, subq_labels)[0] if subq_labels else [x["key"] for x in detail["subquestions"]]
    src = [x for x in detail["subquestions"] if x["key"] in keys]
    weight, types = {}, {x["question_type"] for x in src}
    for x in src:
        for c in x["concepts"]:
            weight[c["concept_id"]] = max(weight.get(c["concept_id"], 0), 2 if c["role"] == "primary" else 1)
    hits, same_type, diff = defaultdict(dict), defaultdict(bool), {}
    for r in candidates(con, list(weight), university, roles=("primary", "secondary")) if weight else []:
        pid, cid = r["problem_id"], r["concept_id"]
        if pid == problem_id or cid not in weight:
            continue
        hits[pid][cid] = max(hits[pid].get(cid, 0), 2 if r["role"] == "primary" else 1)
        same_type[pid] = same_type[pid] or r["question_type"] in types
        diff[pid] = r["problem_difficulty"]
    total, why = {}, {}
    for pid, h in hits.items():
        gap = abs((diff[pid] or 3) - (detail["difficulty"] or 3))
        total[pid] = sum(weight[c] * w for c, w in h.items()) + same_type[pid] - 0.5 * gap
        shared = sorted(h, key=lambda c: -weight[c] * h[c])
        why[pid] = ["共同知识点：" + "、".join(shared[:5]) + (f" 等 {len(shared)} 个" if len(shared) > 5 else "")] + \
            (["同题型"] if same_type[pid] else [])
    return rank(con, total, why, limit=limit)


def also_in(con, problem_ids):
    """
    保留的题还在哪些地方出现过（被标为它的重复题的出处），出题时可以告诉学生这是常考题。

    参数:
        con: 数据库连接
        problem_ids: 保留的题 ID 列表
    返回:
        dict，题目 ID -> list[str]（“学校 年度 卷名 题号”）
    """
    if not problem_ids:
        return {}
    out = defaultdict(list)
    for r in con.execute(f"""
            SELECT p.duplicate_of, d.university, p.admission_year, d.paper_title, p.label
            FROM problems p JOIN documents d ON d.doc_id = p.doc_id
            WHERE p.duplicate_of IN ({','.join('?' * len(problem_ids))}) ORDER BY p.admission_year""", list(problem_ids)):
        out[r["duplicate_of"]].append(f"{r['university']} {r['admission_year']} {r['paper_title']} {r['label']}")
    return dict(out)


def problem_detail(con, problem_id, include_solution=False):
    """
    取一道大题的完整信息：出处、原文、勘误、小问（规范化键、知识点、难度、依赖、解法要点）、插图、原卷页面。

    参数:
        con: 数据库连接
        problem_id: 大题 ID
        include_solution: 是否附带题库中的解答
    返回:
        dict；找不到时返回 None
    """
    p = con.execute("SELECT * FROM problems WHERE problem_id = ?", (problem_id,)).fetchone()
    if not p:
        return None
    d = con.execute("SELECT * FROM documents WHERE doc_id = ?", (p["doc_id"],)).fetchone()
    subs = []
    for s in con.execute("SELECT * FROM subquestions WHERE problem_id = ? ORDER BY seq", (problem_id,)):
        cs = con.execute("""SELECT sc.concept_id, sc.role, c.name FROM subq_concepts sc
                            JOIN concepts c ON c.concept_id = sc.concept_id WHERE sc.subq_id = ?
                            ORDER BY sc.role DESC""", (s["subq_id"],)).fetchall()
        subs.append({"label": s["label"], "key": s["skey"], "summary_zh": s["summary_zh"], "method_zh": s["method_zh"],
                     "question_type": s["question_type"], "difficulty": s["difficulty"],
                     "depends_on": json.loads(s["depends_on"] or "[]"), "concepts": [dict(c) for c in cs]})
    figs = []
    for f in con.execute("SELECT seq, page, kind, description, inner_text, path FROM figures "
                         "WHERE problem_id = ? ORDER BY seq", (problem_id,)):
        f = dict(f)
        f["path"] = os.path.join(DATA_DIR, f["path"]) if f["path"] else ""
        figs.append(f)
    pages_dir = os.environ.get("KAKOMON_PAGES", DEFAULT_PAGES)
    page_images = [os.path.join(pages_dir, p["doc_id"], f"p{n:03d}.jpg") for n in json.loads(p["pages"])]
    return {
        "problem_id": problem_id,
        "source": {"university": d["university"], "graduate_school": d["graduate_school"], "program": d["program"],
                   "admission_year": p["admission_year"], "exam_held": d["exam_held"], "paper_title": d["paper_title"],
                   "section": p["section"], "problem_label": p["label"], "selection_rule": d["selection_rule"]},
        "title_zh": p["title_zh"], "difficulty": p["difficulty"], "est_minutes": p["est_minutes"],
        "content_md": p["content_md"], "errata_md": p["errata_md"], "subquestions": subs, "figures": figs,
        "page_images": [x for x in page_images if os.path.exists(x)],
        "has_solution": bool(p["solution_md"]), "solution_source": p["solution_source"],
        "solution_md": p["solution_md"] if include_solution else "",
        "duplicate_of": p["duplicate_of"] or "", "also_in": also_in(con, [problem_id]).get(problem_id, []),
    }


def resolve_subqs(detail, given):
    """
    把用户或 Claude 写的小问编号对应到题目的规范化键；写的是上一级编号（如 (2)）时展开成它下面的全部小问。

    参数:
        detail: problem_detail 的返回值
        given: 小问编号列表（任意写法）
    返回:
        (list[str] 规范化键, list[str] 对不上的编号)
    """
    keys = [s["key"] for s in detail["subquestions"]]
    out, bad = [], []
    for g in given:
        k = labels.subq_key(g)
        hit = [k] if k in keys else [x for x in keys if x.startswith(k + "-")]
        if not hit:
            bad.append(g)
        out += [h for h in hit if h not in out]
    return out, bad


def subq_rows(detail, keys, secondary_weight=0.4):
    """
    作答记录所需的小问信息：每个小问的知识点权重（主 1.0、次 secondary_weight）、难度、题型。

    参数:
        detail: problem_detail 的返回值
        keys: 规范化小问键
        secondary_weight: 次知识点权重
    返回:
        list[dict]，含 key、label、difficulty、question_type、weights、primary
    """
    out = []
    for s in detail["subquestions"]:
        if s["key"] not in keys:
            continue
        weights = {c["concept_id"]: (1.0 if c["role"] == "primary" else secondary_weight) for c in s["concepts"]}
        out.append({"key": s["key"], "label": s["label"], "difficulty": s["difficulty"] or 3,
                    "question_type": s["question_type"], "weights": weights,
                    "primary": [c["concept_id"] for c in s["concepts"] if c["role"] == "primary"]})
    return out


def problem_meta(con):
    """
    每道题的预计用时、小问数与各小问题型（统计用时比、题型时用）。

    参数:
        con: 数据库连接
    返回:
        dict，题目 ID -> {est_minutes, n_subq, types: {小问键: 题型}}
    """
    out = {}
    for r in con.execute("SELECT problem_id, est_minutes FROM problems"):
        out[r["problem_id"]] = {"est_minutes": r["est_minutes"] or 0, "n_subq": 0, "types": {}}
    for r in con.execute("SELECT problem_id, skey, question_type FROM subquestions"):
        if r["problem_id"] in out:
            out[r["problem_id"]]["n_subq"] += 1
            out[r["problem_id"]]["types"][r["skey"]] = r["question_type"]
    return out


KIND_NOTE = {"compilation": "合订本", "sample_problems": "样题", "single_problem": "单题"}


def year_ranges(years):
    """
    把年度压缩成区间文本，缺的年度一目了然；年度不明（0 或空）的另计。

    参数:
        years: 年度列表
    返回:
        str，如 "2003-2015, 2017-2020, 2023"
    """
    ys = sorted({y for y in years if y})
    out = []
    for y in ys:
        if out and y == out[-1][1] + 1:
            out[-1][1] = y
        else:
            out.append([y, y])
    text = ", ".join(f"{a}-{b}" if b > a else str(a) for a, b in out)
    unknown = sum(1 for y in years if not y)
    return text + (f"（另有 {unknown} 份年度不明）" if unknown else "")


def overview(con, university=""):
    """
    题库概况。不指定学校时按“学校 -> 专攻”汇总试卷份数、大题数和年度；指定学校时再列出每份试卷
    （年度、卷名、大题数）。都附各科目的小问数与总数。

    参数:
        con: 数据库连接
        university: 只看某校（可用简称；多所学校用逗号或加号连接，也可写学校组）；留空看全部学校的汇总
    返回:
        dict
    """
    live = "(p.duplicate_of IS NULL OR p.duplicate_of = '')"
    unis = resolve_universities(university) if university else []
    where = (" AND (" + " OR ".join("d.university LIKE ?" for _ in unis) + ")") if unis else ""
    params = ["%" + u + "%" for u in unis]
    papers = con.execute(f"""
        SELECT d.university, d.program_folder, d.program, d.admission_year, d.paper_title, d.document_kind,
               COUNT(p.problem_id) AS problems, SUM(p.solution_md != '') AS solved
        FROM documents d JOIN problems p ON p.doc_id = d.doc_id WHERE {live}{where}
        GROUP BY d.doc_id ORDER BY d.university, d.program_folder, d.admission_year, d.paper_title""", params).fetchall()
    schools = {}
    for r in papers:
        u = schools.setdefault(r["university"], {"university": r["university"], "papers": 0, "problems": 0,
                                                  "_years": [], "programs": {}})
        folder = r["program_folder"] or r["program"]
        g = u["programs"].setdefault(folder, {"program": folder, "papers": 0, "problems": 0, "_years": [], "list": []})
        for x in (u, g):
            x["papers"] += 1
            x["problems"] += r["problems"]
            x["_years"].append(r["admission_year"])
        if unis:
            own = r["program"] if r["program"] not in folder and folder not in r["program"] else ""
            extra = [n for n in (own, KIND_NOTE.get(r["document_kind"], ""),
                                 f"{r['solved']} 题有解答" if r["solved"] else "") if n]
            g["list"].append(f"{r['admission_year'] or '年度不明'} {r['paper_title']}：{r['problems']} 题" +
                             (f"（{'，'.join(extra)}）" if extra else ""))
    out = []
    for u in sorted(schools.values(), key=lambda x: -x["problems"]):
        progs = []
        for g in sorted(u["programs"].values(), key=lambda x: -x["problems"]):
            item = {"program": g["program"], "papers": g["papers"], "problems": g["problems"],
                    "years": year_ranges(g["_years"])}
            if unis:
                item["papers_list"] = g["list"]
            progs.append(item)
        out.append({"university": u["university"], "papers": u["papers"], "problems": u["problems"],
                    "years": year_ranges(u["_years"]), "programs": progs})
    subjects = [dict(r) for r in con.execute(f"""
        SELECT c.subject, COUNT(*) AS subquestions FROM subq_concepts sc JOIN concepts c ON c.concept_id = sc.concept_id
        JOIN subquestions sq ON sq.subq_id = sc.subq_id JOIN problems p ON p.problem_id = sq.problem_id
        JOIN documents d ON d.doc_id = p.doc_id
        WHERE sc.role = 'primary' AND {live}{where} GROUP BY c.subject ORDER BY subquestions DESC""", params)]
    totals = con.execute(f"""
        SELECT SUM({live}) AS problems, SUM(p.solution_md != '' AND {live}) AS with_solution, SUM(NOT {live}) AS duplicates
        FROM problems p JOIN documents d ON d.doc_id = p.doc_id WHERE 1 {where}""", params).fetchone()
    res = {"problems": totals["problems"] or 0, "with_solution": totals["with_solution"] or 0,
           "duplicates_skipped": totals["duplicates"] or 0, "subjects": subjects, "universities": out}
    if unis:
        res["matched_universities"] = [u["university"] for u in out]
    else:
        res["hint"] = "加 university 参数可列出某校每份试卷的年度、卷名和题数"
    return res
