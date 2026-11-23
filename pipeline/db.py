"""
题库数据库模块：SQLite 表结构与整库重建。

表：documents 试卷；problems 大题（含题目自己的年度、科目、解答、勘误；duplicate_of 指向保留的那道题，非空表示重复题）；subquestions 小问（含规范化键、解法要点、依赖）；
subq_concepts 小问-知识点；concepts 知识点；figures 插图；problems_fts 全文检索（trigram，日中文可检索）。
每次入库都写到临时文件再整体替换；替换前检查学习记录引用的题目和小问是否仍然存在。
"""
import json
import os
import sqlite3

SCHEMA_SQL = """
CREATE TABLE documents (
    doc_id TEXT PRIMARY KEY, rel_path TEXT, university TEXT, graduate_school TEXT, program TEXT,
    admission_year INTEGER, exam_held TEXT, paper_title TEXT, paper_category TEXT, languages TEXT,
    document_kind TEXT, selection_rule TEXT, program_folder TEXT);
CREATE TABLE problems (
    problem_id TEXT PRIMARY KEY, doc_id TEXT REFERENCES documents(doc_id), seq INTEGER, label TEXT, pkey TEXT,
    section TEXT, admission_year INTEGER, year_basis TEXT, title_zh TEXT, content_md TEXT, solution_md TEXT,
    solution_source TEXT, errata_md TEXT, pages TEXT, difficulty INTEGER, est_minutes INTEGER, notes TEXT,
    duplicate_of TEXT DEFAULT '');
CREATE TABLE subquestions (
    subq_id TEXT PRIMARY KEY, problem_id TEXT REFERENCES problems(problem_id), seq INTEGER, label TEXT, skey TEXT,
    summary_zh TEXT, method_zh TEXT, depends_on TEXT, question_type TEXT, difficulty INTEGER);
CREATE TABLE concepts (
    concept_id TEXT PRIMARY KEY, subject_id TEXT, subject TEXT, unit_id TEXT, unit TEXT, name TEXT);
CREATE TABLE subq_concepts (
    subq_id TEXT REFERENCES subquestions(subq_id), concept_id TEXT REFERENCES concepts(concept_id), role TEXT,
    PRIMARY KEY (subq_id, concept_id));
CREATE TABLE figures (
    problem_id TEXT REFERENCES problems(problem_id), seq INTEGER, page INTEGER, kind TEXT, description TEXT,
    inner_text TEXT, path TEXT, PRIMARY KEY (problem_id, seq));
CREATE VIRTUAL TABLE problems_fts USING fts5(problem_id UNINDEXED, title_zh, content_md, summaries, tokenize='trigram');
CREATE INDEX idx_sc_concept ON subq_concepts(concept_id);
CREATE INDEX idx_sq_problem ON subquestions(problem_id);
CREATE INDEX idx_p_doc ON problems(doc_id);
"""

DOC_FIELDS = ["doc_id", "rel_path", "university", "graduate_school", "program", "admission_year", "exam_held",
              "paper_title", "paper_category", "languages", "document_kind", "selection_rule", "program_folder"]


def learner_refs(learner_path):
    """
    学习记录里引用过的题目与小问。

    参数:
        learner_path: 学习记录库路径
    返回:
        (set 题目 ID, set (题目 ID, 小问键))
    """
    if not learner_path or not os.path.exists(learner_path):
        return set(), set()
    con = sqlite3.connect(learner_path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    pids, subqs = set(), set()
    for t in ("served", "attempts", "cards"):
        if t in tables:
            pids |= {r[0] for r in con.execute(f"SELECT DISTINCT problem_id FROM {t}")}
    for t in ("attempts", "cards"):
        if t in tables and "subq_key" in {r[1] for r in con.execute(f"PRAGMA table_info({t})")}:
            subqs |= {(r[0], r[1]) for r in con.execute(f"SELECT DISTINCT problem_id, subq_key FROM {t}")}
    con.close()
    return pids, subqs


def build_bank(path, docs, doc_rows, tags, concepts, data_dir, learner_path=None, force=False, duplicates=None):
    """
    整库重建题库：先写临时文件，检查学习记录引用后再替换正式文件。

    参数:
        path: 题库路径
        docs: dict，doc_id -> assemble.build_doc 结果（已挂接、已裁图）
        doc_rows: dict，doc_id -> 清单文档行
        tags: dict，problem_id -> 校验后的标签
        concepts: tagging.load_taxonomy 返回的知识点字典
        data_dir: data/ 目录（插图路径存为相对它的路径）
        learner_path: 学习记录库路径（检查引用用）
        force: 引用缺失时也替换
        duplicates: dict，重复题 ID -> 保留的题 ID（link.duplicate_map 的结果）
    返回:
        dict，含 problems、subquestions、skipped（缺标签未入库的题）、missing_refs
    """
    duplicates = duplicates or {}
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA_SQL)
    con.executemany("INSERT INTO concepts VALUES (?,?,?,?,?,?)",
                    [(c["concept_id"], c["subject_id"], c["subject"], c["unit_id"], c["unit"], c["name"])
                     for c in concepts.values()])
    stats = {"problems": 0, "subquestions": 0, "skipped": []}
    new_pids, new_subqs = set(), set()
    for doc_id, d in docs.items():
        row = doc_rows[doc_id]
        con.execute(f"INSERT INTO documents VALUES ({','.join('?' * len(DOC_FIELDS))})",
                    [row.get(k) for k in DOC_FIELDS])
        for p in d["problems"]:
            t = tags.get(p["problem_id"])
            if not t:
                stats["skipped"].append(p["problem_id"])
                continue
            con.execute("INSERT INTO problems VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (p["problem_id"], doc_id, p["seq"], p["label"], p["pkey"], p["section"], p["admission_year"],
                         p["year_basis"], t.get("title_zh", p["title_hint"]), p["content_md"], p["solution_md"],
                         p["solution_source"], p["errata_md"], json.dumps(p["pages"]), t.get("difficulty"),
                         t.get("est_minutes"), t.get("notes", ""), duplicates.get(p["problem_id"], "")))
            summaries = "\n".join(f"{sq['summary_zh']} {sq['method_zh']}" for sq in t["subquestions"])
            con.execute("INSERT INTO problems_fts VALUES (?,?,?,?)",
                        (p["problem_id"], t.get("title_zh", ""), p["content_md"], summaries))
            for k, f in enumerate(p["figures"], 1):
                rel = os.path.relpath(f["path"], data_dir) if f.get("path") else ""
                con.execute("INSERT INTO figures VALUES (?,?,?,?,?,?,?)",
                            (p["problem_id"], k, f["page"], f["kind"], f["description"], f.get("inner_text", ""), rel))
            for k, sq in enumerate(t["subquestions"], 1):
                sid = f"{p['problem_id']}-{sq['key']}"
                con.execute("INSERT INTO subquestions VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (sid, p["problem_id"], k, sq["label"], sq["key"], sq["summary_zh"], sq["method_zh"],
                             json.dumps(sq["depends_on"]), sq["question_type"], sq["difficulty"]))
                if sq["primary_concept"] in concepts:
                    con.execute("INSERT OR IGNORE INTO subq_concepts VALUES (?,?,?)", (sid, sq["primary_concept"], "primary"))
                for c in sq["secondary_concepts"]:
                    con.execute("INSERT OR IGNORE INTO subq_concepts VALUES (?,?,?)", (sid, c, "secondary"))
                new_subqs.add((p["problem_id"], sq["key"]))
                stats["subquestions"] += 1
            new_pids.add(p["problem_id"])
            stats["problems"] += 1
    con.commit()
    con.close()
    old_pids, old_subqs = learner_refs(learner_path)
    missing = sorted(old_pids - new_pids) + sorted(f"{a}#{b}" for a, b in old_subqs - new_subqs)
    stats["missing_refs"] = missing
    if missing and not force:
        stats["replaced"] = False
        return stats
    os.replace(tmp, path)
    stats["replaced"] = True
    return stats
