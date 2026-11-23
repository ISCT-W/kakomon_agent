"""
过去问学习助手的 MCP 服务（stdio）：知识点表与别名、分层找题、按知识点出题（自动扩展到相邻知识点）、
按小问记录作答（提示次数、用时、自报结果、跳过原因）、掌握度与复习排期、目标与重要期间、学习计划、周期报告、
学习画像（数据摘要＋观察记录＋解读）、题目报错、解答存档（AI 解答保存后复用）、题目面板。

题库默认读取 data/bank.sqlite（环境变量 KAKOMON_DB 可改），学习记录写入 data/learner.sqlite
（KAKOMON_LEARNER_DB），画像写入 data/learner_profile.md（KAKOMON_PROFILE），解答存档在 data/solutions/（KAKOMON_SOLUTIONS）。
启动时会在 127.0.0.1:18766 提供题目面板（data/viewer/）。

用法（由 Claude Code 通过 .mcp.json 启动）:
    python -I agent/server.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp.server.mcpserver import MCPServer  # noqa: E402

import bank  # noqa: E402
import insights  # noqa: E402
import learner  # noqa: E402
import solutions  # noqa: E402
import tutor  # noqa: E402
import viewer  # noqa: E402

INSTRUCTIONS = """日本大学院入试（情報系）过去问题库与学习记录。
会话开始先用 get_profile 和 list_goals 了解学生；第一次练习前读一次 list_taxonomy(compact=true)，按语义把学生的说法对应到知识点 ID。
每次只出一道大题：按知识点练习用 next_question；找题用 find_problems / similar_problems，读结果挑选后用 serve_problem 出题。
学生反馈后用 record_attempt 按小问记录（含提示次数），再出下一题。
讲解先用 get_problem(include_solution=true) 查已有解答并复用；自己写的解答用 save_solution 保存。
和学生交流一律用中文（学生改用别的语言时跟随学生），回答简洁；题目原文照抄日文。"""

server = MCPServer("kakomon", instructions=INSTRUCTIONS)
BANK = bank.connect()
LEARN = learner.connect()
VIEWER_URL = viewer.start_server()
MAX_RESULT_CHARS = 30000
MAX_FIND_LIMIT = 30


def dumps(obj, compact=False):
    """
    把结果序列化为 JSON 文本。

    参数:
        obj: 任意可序列化对象
        compact: 不缩进（结果较长时用，节省篇幅）
    返回:
        str
    """
    if compact:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(obj, ensure_ascii=False, indent=1)


def annotate(rows):
    """
    给候选题标上学生的作答情况：done 是否出过，last_result 最近一次结果。

    参数:
        rows: bank.find_problems / bank.similar_problems 的结果
    返回:
        list[dict]，原地修改后的 rows
    """
    served, history = learner.served_problems(LEARN), learner.problem_history(LEARN)
    for r in rows:
        r["done"] = r["problem_id"] in served
        r["last_result"] = history.get(r["problem_id"], {}).get("last", "")
    return rows


def fit(out):
    """
    把列表结果序列化，超过 MAX_RESULT_CHARS 时去掉排在后面的候选（避免 Claude Code 把结果存成文件）。

    参数:
        out: dict，含 results 列表
    返回:
        str
    """
    text = dumps(out, compact=True)
    while len(text) > MAX_RESULT_CHARS and len(out["results"]) > 1:
        out["results"] = out["results"][:max(1, len(out["results"]) * 3 // 4)]
        out["shown"] = len(out["results"])
        out["hint"] = "结果太长，只列出得分最高的几道；需要更多时加限制条件再查"
        text = dumps(out, compact=True)
    return text


def finish(out):
    """
    出题结果的收尾：更新题目面板、补上面板地址，去掉内部字段后序列化。

    参数:
        out: tutor.next_question / tutor.serve_problem 的返回值
    返回:
        str
    """
    if out.get("found"):
        viewer.publish([viewer.item(out["_detail"], out["_focus_keys"])], note=out["_note"])
        out["viewer_url"] = VIEWER_URL
    return dumps({k: v for k, v in out.items() if not k.startswith("_")})


@server.tool()
def bank_overview(university: str = "") -> str:
    """
    查看题库概况：总题数、有解答的题数、各科目的小问数，以及各校各专攻收录的试卷（份数、大题数、年度区间，
    缺的年度一目了然）。指定学校时再列出该校每份试卷（年度、卷名、大题数）。
    学生问“题库里有什么”“某校收录了哪些年度”时使用。

    参数:
        university: 只看某校（可用简称、英文名、新校名；多所学校用逗号或加号连接，也可写学校组，如“七帝大”）；
                    留空看全部学校的汇总
    """
    return dumps(bank.overview(BANK, university), compact=True)


@server.tool()
def list_taxonomy(subject_id: str = "", only_available: bool = True, compact: bool = False) -> str:
    """
    知识点表（科目 -> 单元 -> 知识点）及每个知识点在题库中的小问数 n。

    参数:
        subject_id: 只看某个科目，如 la（线性代数）、calc（微积分）、algo（算法），留空看全部
        only_available: 只显示题库里有题的知识点（默认 true；compact 时忽略）
        compact: 返回紧凑文本形式的知识点表（每行一个知识点，附日文/英文/中文别名与题量；全部约 2.7 万字，
                 只看一个科目时短得多）。会话中第一次需要知识点 ID 时读一次，之后按语义把学生的说法对应到知识点 ID
    """
    if compact:
        return bank.taxonomy_compact(BANK, subject_id)
    data = bank.taxonomy(BANK, subject_id)
    if only_available:
        data = [dict(s, units=[dict(u, concepts=[c for c in u["concepts"] if c["n"]]) for u in s["units"] if u["n"]])
                for s in data if s["n"]]
    return dumps(data)


@server.tool()
def search_concepts(keyword: str) -> str:
    """
    按关键词快速查找知识点 ID：匹配知识点、单元、科目的名称、ID 以及别名（日文术语、英文、中文同义词），
    忽略全半角、空格和大小写。例：“特征值”“固有値”“eigenvalue”“DP”“デッドロック”。返回结果中的 n 是题库里的小问数。
    查不到或说法模糊时，改为读 list_taxonomy(compact=true) 按语义对应。

    参数:
        keyword: 关键词
    """
    return dumps(bank.search_concepts(BANK, keyword))


@server.tool()
def find_problems(concepts: list[str] | None = None, keywords: list[str] | None = None, university: str = "",
                  program: str = "", question_type: str = "", difficulty_min: int = 0, difficulty_max: int = 0,
                  limit: int = 20, brief: bool = False) -> str:
    """
    分层找题的检索层：按知识点（主/次）和关键词召回候选大题，再按学校、专攻、题型、难度过滤，返回紧凑信息
    （出处、每个小问一行“编号｜题型｜难度｜主知识点｜摘要｜解法要点”、是否做过）。由你阅读这些候选、按学生的需求挑选，
    不满意就换词再查；选定后用 serve_problem 出题。学生想找“考过 XX 的题”“和某道题类似的题”、或要找的内容不对应
    单个知识点时使用。找相似题：把原题小问的主知识点（get_problem 的 subquestions[].concepts）一起传给 concepts，
    共同知识点越多得分越高。摘要和解法要点会暴露考点，只供你判断，不要展示给学生。

    参数:
        concepts: 知识点 ID 或前缀（先用知识点表对应好）
        keywords: 关键词。原文多为日文，日文术语召回原文（如“固有値”“二分探索木”“デッドロック”）；
                  中文词召回中文标题和小问摘要（如“特征值”“动态规划”）。可以同时给日文和中文
        university: 只查某校（可用简称、英文名）；多所学校用逗号或加号连接，也可写学校组，如“七帝大+东科大”
        program: 专攻关键词（中文、英文、日文均可）
        question_type: 只要含这种题型小问的题：calculation、proof、explanation、programming、code_reading、design、fill_in、choice
        difficulty_min: 小问难度下限（1-5，0 不限）
        difficulty_max: 小问难度上限（1-5，0 不限）
        limit: 最多返回几道（默认 20，最多 30；结果太长时会自动少返回几道）
        brief: 只返回出处、标题和难度，不列小问（候选多、先粗看时用）
    """
    if not concepts and not keywords:
        return dumps({"error": "至少给 concepts（知识点 ID 列表）或 keywords（关键词列表）之一"})
    limit = max(1, min(limit or 20, MAX_FIND_LIMIT))
    rows, total = bank.find_problems(BANK, concepts or [], keywords or [], university, program, question_type,
                                     difficulty_min, difficulty_max, limit, brief)
    out = {"total": total, "shown": len(rows), "results": annotate(rows)}
    if not total:
        out["hint"] = "换成日文术语或同义词、减少关键词、去掉学校/专攻/题型限制，或换相邻的知识点"
    return fit(out)


@server.tool()
def similar_problems(problem_id: str, subq_labels: list[str] | None = None, university: str = "",
                     limit: int = 8) -> str:
    """
    找和某道题（或其中几个小问）相似的过去问：按共同知识点（主知识点权重高）、同题型、难度接近打分，
    排除原题和学生已经掌握（最近一次全会）的题，附各小问摘要供你挑选。学生问“有没有类似的题”时使用。
    摘要和解法要点只供你判断，向学生介绍时只说出处和一句不涉及解法的描述。

    参数:
        problem_id: 原题 ID
        subq_labels: 只按这几个小问找（学生只问某一小问时），留空按整道题
        university: 只在这些学校里找（可用简称、学校组，如“七帝大+东科大”），留空不限
        limit: 返回几道（默认 8，最多 30）
    """
    limit = max(1, min(limit or 8, MAX_FIND_LIMIT))
    found = bank.similar_problems(BANK, problem_id, subq_labels or [], university, limit + 10)
    if found is None:
        return dumps({"error": f"找不到 {problem_id}"})
    rows, total = found
    history = learner.problem_history(LEARN)
    rows = [r for r in rows if not (history.get(r["problem_id"], {}).get("last") == "会"
                                    and not history[r["problem_id"]]["bad_keys"])][:limit]
    return fit({"total": total, "shown": len(rows), "results": annotate(rows)})


@server.tool()
def next_question(concepts: list[str], university: str = "", program: str = "", mode: str = "adaptive",
                  allow_seen: bool = False, expand: bool = True) -> str:
    """
    按知识点出下一道题（一次一道大题），并同步到题目面板。所选知识点的新题出完时自动扩展：
    先找把它作为次要考点的题，再找相邻知识点（同单元或常一起出现）的题，最后提前复习做过的题。

    参数:
        concepts: 知识点 ID 或前缀列表，如 ["la.eigen"]（整个单元）、["calc.mint.change"]（单个知识点）、["la"]（整个科目）
        university: 只出某校的题（可用简称、英文名，如 京大、东大、东科大/东工大、Science Tokyo）；多所学校用逗号或加号连接，
                    也可写学校组（七帝大/旧帝大 = 7 所旧帝国大学），如“七帝大+东科大”；留空不限
        program: 专攻关键词（中文、英文、日文均可，如“计算机”“通信情報”），留空不限；返回 matched_programs 供确认
        mode: adaptive（默认）：先到期复习，再挑预测做对概率约 70% 的新题，刚接触的知识点从最简单的开始；
              ascending：只出新题，从最简单的开始；review：只出做过的题（到期的优先，其次之前不会的）
        allow_seen: 是否允许把出过的题当新题再出（默认否）
        expand: 新题出完时是否扩展到相邻知识点（默认是）
    返回:
        JSON：出处、原文 content_md、勘误 errata_md、本次重点小问 focus、需要先做的前置小问 prerequisite_labels、
        来源 pool 与层级 level、扩展说明 expansion（扩展到相邻知识点时要告诉学生）、预测做对概率、插图与原卷页面、
        题目面板地址；没有合适的题时 found=false 并说明原因
    """
    out = tutor.next_question(BANK, LEARN, concepts, university, program, mode, allow_seen, expand)
    return finish(out)


@server.tool()
def serve_problem(problem_id: str, subq_labels: list[str] | None = None) -> str:
    """
    把指定的一道题出给学生（用于 find_problems 找到的题、学生点名要做的题）：记录出题并更新题目面板。

    参数:
        problem_id: 大题 ID
        subq_labels: 本次要做的小问编号（任意写法，如 ["(1)"] 会展开成 (1)(a)、(1)(b)……）；留空表示整道题
    返回:
        JSON，格式同 next_question
    """
    return finish(tutor.serve_problem(BANK, LEARN, problem_id, subq_labels))


@server.tool()
def get_problem(problem_id: str, include_solution: bool = False) -> str:
    """
    取一道大题的完整信息：出处、原文、勘误、全部小问（知识点、难度、题型、依赖、解法要点）、插图、原卷页面路径，
    以及已有哪些解答（solutions：bank 题库收录 / human 人工 / ai 之前保存的 AI 解答）。
    讲解、给提示前调用；解法要点只供你参考，不要在学生做完前说出来。

    参数:
        problem_id: 大题 ID
        include_solution: 附带一份解答（solution 字段，优先级：题库收录 > 人工 > AI）；讲解时设为 true
    """
    detail = bank.problem_detail(BANK, problem_id, include_solution)
    if not detail:
        return dumps({"error": f"找不到 {problem_id}"})
    detail["solutions"] = solutions.available(problem_id, detail.pop("has_solution"))
    bank_md, source = detail.pop("solution_md"), detail.pop("solution_source")
    if include_solution:
        detail["solution"] = solutions.best(problem_id, bank_md, source)
    return dumps(detail)


@server.tool()
def save_solution(problem_id: str, markdown: str, subq_labels: list[str] | None = None, verified: str = "",
                  append: bool = False) -> str:
    """
    保存你写的解答（存为 AI 解答 data/solutions/<题目 ID>.ai.md，与人工解答 .human.md 分开），并显示到题目面板。
    自己写了解答就必须保存；之后同一道题直接用 get_problem 取出复用，不要重写。

    参数:
        problem_id: 大题 ID
        markdown: 完整解答（中文 Markdown，公式用 LaTeX；不用写“AI 解答”字样，存档和面板会标注）
        subq_labels: 只讲了部分小问时写编号，留空表示整道题
        verified: 核对情况，如“sympy 核对了行列式与特征值”；没核对留空
        append: 补讲其他小问时设为 true（接在已存解答后面）；学生指出错误、整份重写时为 false（覆盖）
    """
    detail = bank.problem_detail(BANK, problem_id)
    if not detail:
        return dumps({"error": f"找不到 {problem_id}"})
    keys, bad = bank.resolve_subqs(detail, subq_labels or [])
    names = {s["key"]: s["label"] for s in detail["subquestions"]}
    sol = solutions.save_ai(problem_id, markdown, [names[k] for k in keys] + bad, verified, append)
    viewer.publish([viewer.item(detail, solution=sol)], note="解答")
    path, root = solutions.path_of(problem_id, "ai"), os.path.dirname(solutions.DATA_DIR)
    return dumps({"saved": True, "file": os.path.relpath(path, root) if path.startswith(root + os.sep) else path,
                  "covers": sol["covers"], "viewer_url": VIEWER_URL})


@server.tool()
def show_problems(problem_ids: list[str], include_solution: bool = False) -> str:
    """
    把一道或几道题（公式渲染好）显示到题目面板，只是展示，不算出题、不计时。向学生列举例题、对照讲解时使用。

    参数:
        problem_ids: 大题 ID 列表（按显示顺序）
        include_solution: 同时显示解答（优先级同 get_problem）
    """
    items, missing, unsolved = [], [], []
    for pid in problem_ids:
        d = bank.problem_detail(BANK, pid, include_solution)
        if not d:
            missing.append(pid)
            continue
        sol = solutions.best(pid, d["solution_md"], d["solution_source"]) if include_solution else None
        if include_solution and not sol:
            unsolved.append(pid)
        items.append(viewer.item(d, solution=sol))
    if items:
        viewer.publish(items, note="展示（未出题）")
    out = {"shown": [i["problem_id"] for i in items], "viewer_url": VIEWER_URL}
    if missing:
        out["missing"] = missing
    if unsolved:
        out["no_solution"] = unsolved
    return dumps(out)


@server.tool()
def record_attempt(problem_id: str, results: dict[str, str] | None = None, result: str = "",
                   subq_labels: list[str] | None = None, hints: int = 0, solution_viewed: bool = False,
                   minutes: float | None = None, self_report: str = "", error_type: str = "", note: str = "",
                   verified: bool = False, skip_reason: str = "") -> str:
    """
    记录学生对一道题的作答反馈（每个小问一条）：更新掌握度、安排各小问的下次复习时间。

    参数:
        problem_id: 大题 ID
        results: 各小问的结果，如 {"(2)(a)": "会", "(2)(b)": "不会"}；小问结果不同时用它
        result: 所有小问同一个结果时用：会 / 部分会 / 不会 / 跳过
        subq_labels: 与 result 搭配的小问编号；留空时沿用最近一次出题（6 小时内、且就是这道题）的重点小问
        hints: 做这些小问时看过几次提示（0 表示没看）；看了提示才做出来的“会”会打折
        solution_viewed: 作答前看过完整讲解（这时最多算 0.25 分）
        minutes: 学生报的用时（分钟，整次作答合计）；不报时按出题到记录的时间估计
        self_report: 批改前学生自己说的结果（如自称“会”，批改后发现有错，就填“会”，result 填批改后的结果）
        error_type: 不会或部分会时的错因：没思路 / 卡在中间步骤 / 计算错误 / 概念不清 / 忘记公式 / 读题有误 / 时间不够 / 其他
        note: 备注（学生原话、卡住的位置）
        verified: 是否经过批改确认（学生给出了答案或关键结果且你核对过为 true；仅自报为 false）
        skip_reason: 跳过的原因：太难 / 没时间 / 不想做这类 / 已经会了 / 其他
    返回:
        JSON：每个小问的结果、得分与下次复习时间，各知识点当前掌握度的前后变化
    """
    return dumps(tutor.record_attempt(BANK, LEARN, problem_id, results, result, subq_labels, hints, solution_viewed,
                                      minutes, self_report, error_type, note, verified, skip_reason))


@server.tool()
def get_progress(concept_prefix: str = "") -> str:
    """
    掌握情况：各知识点的当前掌握度 mastery（已按遗忘衰减）、历史最高 mastery_peak、等级（未接触/薄弱/进步中/掌握）、
    作答量、平均得分、最近结果、错因、到期复习数；以及按题型的得分、跳过、看提示比例、用时比。

    参数:
        concept_prefix: 只看某个科目或单元，如 la、calc.mint；留空看全部
    """
    return dumps(insights.progress(BANK, LEARN, concept_prefix))


@server.tool()
def review_queue(concept_prefix: str = "") -> str:
    """
    列出已到期、需要复习的题（最早到期的在前），附出处、到期的小问与上次结果。学生问“今天要复习什么”时使用。

    参数:
        concept_prefix: 只看涉及某个科目或单元的题，留空看全部
    """
    due = learner.due_cards(LEARN, concept_prefixes=[concept_prefix] if concept_prefix else None)
    for d in due:
        p = bank.problem_detail(BANK, d["problem_id"]) or {"subquestions": [], "source": {}}
        labels = {s["key"]: s["label"] for s in p["subquestions"]}
        d["labels"] = [labels.get(k, k) for k in d.pop("keys")]
        d["source"] = p["source"]
    return dumps({"due": due, "count": len(due)})


@server.tool()
def set_goal(exam_date: str, universities: list[str], programs: list[str] | None = None, note: str = "") -> str:
    """
    设定备考目标（会替换旧目标）：考试日期、目标学校与专攻。学习计划和周期报告会据此排序与倒计时。
    学生说出考试时间和学校就直接保存（之后可以再改），不必先等学生确认。

    参数:
        exam_date: 考试日期 YYYY-MM-DD；只知道月份时写 YYYY-MM（倒计时按当月 1 日算）
        universities: 目标学校（可用简称、英文名、学校组，如 ["京大", "东大"]、["七帝大"]）
        programs: 目标专攻关键词（可选，如 ["通信情報", "计算机"]）
        note: 备注
    """
    if not learner.exam_day(exam_date):
        return dumps({"saved": False, "error": "考试日期写成 YYYY-MM-DD，只知道月份时写 YYYY-MM"})
    unis = bank.resolve_universities(universities)
    gid = learner.set_goal(LEARN, exam_date, unis, programs or [], note)
    progs = sorted({p for k in programs or [] for u in unis or [""] for p in bank.matched_programs(BANK, k, u)})
    return dumps({"saved": True, "id": gid, "universities": unis, "programs": programs or [],
                  "matched_programs": progs, "exam_date": exam_date})


@server.tool()
def add_period(name: str, start_date: str, end_date: str, focus_concepts: list[str] | None = None,
               note: str = "") -> str:
    """
    添加一个重要期间（冲刺期、复习周、考前一周等）。期间内学习计划会优先其重点知识点，期间结束时适合做一次总结。

    参数:
        name: 名称，如“线代冲刺周”
        start_date: 开始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD
        focus_concepts: 重点知识点 ID 或前缀（可选，如 ["la", "calc.mint"]）
        note: 备注
    """
    pid = learner.add_period(LEARN, name, start_date, end_date, focus_concepts or [], note)
    return dumps({"saved": True, "id": pid})


@server.tool()
def list_goals() -> str:
    """
    查看当前备考目标（含考试倒计时）以及进行中和未开始的重要期间。
    """
    plan = insights.study_plan(BANK, LEARN, top_n=0)
    return dumps({"goal": learner.active_goal(LEARN), "days_to_exam": plan["days_to_exam"],
                  "periods": learner.list_periods(LEARN), "due_reviews": plan["due_reviews"]})


@server.tool()
def study_plan(university: str = "", program: str = "", subjects: list[str] | None = None, top_n: int = 8) -> str:
    """
    给出接下来最该练的知识点：按“目标学校过去问中的出题频率（近年加权）×（1 - 当前掌握度）”排序，
    进行中期间的重点知识点加权，有到期复习题的加分。每项附理由；notice 不为空时要转告学生。
    学生问“今天练什么”“接下来学什么”时使用；只问某校考什么（不涉及掌握度）时用 exam_trends。

    参数:
        university: 临时指定学校（可用简称；多所学校用逗号或加号连接，也可写学校组）；留空则使用 set_goal 设定的目标。
                    只写校名，专攻写在 program 里
        program: 临时指定专攻关键词（中文、英文、日文均可，如“计算机”“情報工学”）；留空时：指定了学校就看该校全部专攻，
                 没指定学校就用目标里的专攻
        subjects: 只看这些科目：科目 ID（la、calc、prob……）、中文名，或科目组“数学”“计算机”
        top_n: 返回前几个知识点
    """
    unis = bank.resolve_universities(university) if university else None
    progs = [program] if program else None
    return dumps(insights.study_plan(BANK, LEARN, unis, progs, top_n, subjects=subjects))


@server.tool()
def exam_trends(university: str = "", program: str = "", subjects: list[str] | None = None, top_n: int = 15) -> str:
    """
    出题趋势：某校（某专攻、某些科目）过去问里各知识点考过哪些年度、共几道大题，按考过的年数排序；
    scope 给出统计范围（匹配到的专攻、收录年度、大题数）。学生问“某校常考什么”“某科考哪些内容”时使用，常考点以它为准。

    参数:
        university: 学校（可用简称、学校组），留空为全部学校；只写校名，专攻写在 program 里
        program: 专攻关键词（中文、英文、日文均可），留空不限
        subjects: 科目 ID（la、calc、prob……）、中文名，或科目组“数学”“计算机”，留空不限
        top_n: 返回前几个知识点
    """
    return dumps(bank.exam_trends(BANK, university, program, subjects or [], top_n), compact=True)


@server.tool()
def progress_report(days: int = 7) -> str:
    """
    周期报告数据：最近 days 天的作答量与结果、学习天数、各科与各题型表现、看提示次数、掌握度变化、错因分布、
    学习习惯（会话次数、时长、时段、连续天数）、到期复习数、考试倒计时与重要期间。学生要周报、期间总结、阶段复盘时使用。

    参数:
        days: 统计最近几天（默认 7）
    """
    return dumps(insights.report(BANK, LEARN, days))


@server.tool()
def get_profile() -> str:
    """
    读取学生的学习画像。会话开始时调用一次，用来个性化出题与讲解。返回三部分：
    facts（学习数据摘要，由作答记录现算，每条有编号，只列样本足够的事实）、observations（会话中记下的观察，
    source 为 student 的是学生本人的说法或纠正）、profile（你之前写的画像解读）；stale 为 true 时本次会话结束前应更新画像。
    """
    return dumps(insights.profile_payload(BANK, LEARN))


@server.tool()
def save_profile(markdown: str) -> str:
    """
    保存更新后的画像解读（旧版本自动存档）。用中文 Markdown，400 字以内，按“强项 / 薄弱点 / 错误习惯 / 学习偏好 /
    下次建议”组织；每条结论注明依据（数据摘要的编号如 F3，或观察记录的日期），学生本人的纠正优先。

    参数:
        markdown: 新的画像解读
    """
    n = len([a for a in learner.all_attempts(LEARN) if a["result"] != "跳过"])
    learner.save_profile(LEARN, markdown, n)
    return dumps({"saved": True, "path": learner.profile_path(), "chars": len(markdown), "attempts_at": n})


@server.tool()
def note_observation(text: str, source: str = "claude") -> str:
    """
    记一条观察：会话中看到的、作答数据反映不出来的事（学生说的目标与顾虑、偏好的讲解方式、情绪、卡住的方式），
    或学生对画像的纠正。一句话，写具体。写画像解读时会用到。

    参数:
        text: 观察内容
        source: claude（你观察到的）或 student（学生本人说的，包括纠正）
    """
    if source not in ("claude", "student"):
        source = "claude"
    oid = learner.add_observation(LEARN, text, source)
    return dumps({"recorded": True, "id": oid})


@server.tool()
def report_issue(problem_id: str, description: str) -> str:
    """
    记录题目转写或知识点标签的错误（学生说“这题转写错了”“公式不对”时使用）。有未处理报错的题会暂停出题，
    之后人工修正（agent/issues.py）。

    参数:
        problem_id: 大题 ID
        description: 问题描述（哪一处、原卷是什么、转写成了什么）
    """
    rid = learner.report_issue(LEARN, problem_id, description)
    return dumps({"recorded": True, "id": rid, "note": "这道题在报错处理前不会再出"})


if __name__ == "__main__":
    server.run("stdio")
