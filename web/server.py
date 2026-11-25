"""
kakomon 学习界面的本地服务：网页（web/static）、聊天中转（每条消息交给 headless Claude Code 运行，流式转发给浏览器）、
界面数据接口（目标、掌握度、倾向、复习、题目区、学习概况）。

智能体本体不变：仍是 Claude Code ＋ kakomon MCP 工具＋ kakomon-tutor 技能，在学习目录（agent/setup_study.py 建，
默认 ~/kakomon-study）里运行。只监听 127.0.0.1；每次启动生成随机令牌，接口请求必须带上，并检查 Host 头，
防止别的网页借浏览器调用本机接口。

用法:
    python -I web/server.py [--port 18770] [--sandbox 目录] [--study 目录]
    --sandbox：学习记录、画像、解答存档、题目区数据都放到这个目录（试用或测试，不碰正式记录）
    --study：学习目录（默认环境变量 KAKOMON_STUDY_DIR 或 ~/kakomon-study，不存在时自动建）
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "web", "static")
sys.path.insert(0, os.path.join(ROOT, "agent"))

LABELS = {"Skill": "加载刷题流程", "get_profile": "读取学习画像", "list_goals": "读取备考目标", "set_goal": "保存目标",
          "exam_trends": "统计常考点", "study_plan": "排学习优先级", "bank_overview": "查看题库", "list_taxonomy": "读知识点表",
          "search_concepts": "查知识点", "find_problems": "检索题目", "similar_problems": "找相似题", "show_problems": "显示题目",
          "next_question": "出题", "serve_problem": "出题", "get_problem": "读取题目与已有解答", "record_attempt": "记录作答",
          "save_solution": "保存 AI 解答", "get_progress": "查看进度", "review_queue": "查看复习", "progress_report": "生成报告",
          "save_profile": "更新画像", "note_observation": "记下观察", "report_issue": "报告题目错误", "add_period": "添加期间",
          "Bash": "用 Python 核对", "Read": "读取文件"}
KEEP_DATA = {"exam_trends", "similar_problems", "set_goal", "record_attempt", "serve_problem", "next_question",
             "show_problems", "save_solution"}
UI_NOTE = ("你在 kakomon 学习界面里运行：界面左边是对话，中间是题目区，右边是学生的目标、掌握度和复习。"
           "show_problems、serve_problem、next_question、save_solution 的内容会自动显示在中间的题目区（题目原文渲染好、原卷并排、"
           "完整解答），exam_trends 的统计会显示在题目区的“计划”页。所以：出题或列题时不要在对话里照抄题目原文和勘误"
           "（这一条优先于 kakomon-tutor 技能第 3 节的“照抄 content_md”），只写出处、本次重点小问、难度和预计用时；"
           "讲解时对话里只写结论和关键一步；不要贴面板链接，说“在中间的题目区”即可。"
           "学生也可以在题目区按小问点选“会 / 部分会 / 不会”，提交后会以“3) 会，4) 不会”这样的消息发给你，照常记录。")


class App:
    """
    服务的配置与运行状态：数据路径、学习目录、Claude Code 会话、正在运行的那一轮。
    """

    def __init__(self, port, sandbox, study):
        """
        准备数据路径（--sandbox 时指向该目录）、界面状态目录、学习目录和 MCP 配置。

        参数:
            port: 服务端口
            sandbox: 试用目录（空表示用正式数据）
            study: 学习目录（空表示环境变量 KAKOMON_STUDY_DIR 或 ~/kakomon-study）
        """
        self.port, self.token = port, secrets.token_urlsafe(24)
        data = os.path.join(ROOT, "data")
        if sandbox:
            sandbox = os.path.abspath(os.path.expanduser(sandbox))
            os.makedirs(sandbox, exist_ok=True)
            os.environ.update({"KAKOMON_LEARNER_DB": os.path.join(sandbox, "learner.sqlite"),
                               "KAKOMON_PROFILE": os.path.join(sandbox, "profile.md"),
                               "KAKOMON_SOLUTIONS": os.path.join(sandbox, "solutions"),
                               "KAKOMON_VIEWER_DIR": os.path.join(sandbox, "viewer")})
        self.state_dir = os.path.join(sandbox or data, "ui")
        os.makedirs(self.state_dir, exist_ok=True)
        self.viewer_file = os.path.join(os.environ.get("KAKOMON_VIEWER_DIR", os.path.join(data, "viewer")), "current.json")
        self.study = os.path.abspath(os.path.expanduser(study or os.environ.get("KAKOMON_STUDY_DIR", "~/kakomon-study")))
        if not os.path.exists(os.path.join(self.study, ".claude", "skills", "kakomon-tutor")):
            subprocess.run([sys.executable, "-I", os.path.join(ROOT, "agent", "setup_study.py"), self.study], check=True)
        env = {k: os.environ[k] for k in ("KAKOMON_LEARNER_DB", "KAKOMON_PROFILE", "KAKOMON_SOLUTIONS", "KAKOMON_VIEWER_DIR",
                                          "KAKOMON_DB") if k in os.environ}
        env["KAKOMON_VIEWER_PORT"] = str(port)
        self.mcp_config = os.path.join(self.state_dir, "mcp.json")
        with open(self.mcp_config, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"kakomon": {"type": "stdio", "command": os.path.join(self.study, ".venv", "bin", "python"),
                                                  "args": ["-I", os.path.join(ROOT, "agent", "server.py")], "env": env}}},
                      f, ensure_ascii=False, indent=2)
        import bank
        import learner
        self.bank, self.learn = bank.connect(), learner.connect()
        self.lock, self.proc = threading.Lock(), None

    def path(self, name):
        """
        界面状态文件路径（会话 ID、对话记录）。

        参数:
            name: 文件名
        返回:
            str
        """
        return os.path.join(self.state_dir, name)

    def session(self):
        """
        当前 Claude Code 会话 ID（没有时为空字符串）。

        返回:
            str
        """
        try:
            return json.load(open(self.path("session.json"), encoding="utf-8")).get("id", "")
        except (OSError, ValueError):
            return ""

    def history(self):
        """
        当前对话的记录（刷新网页后用来恢复对话和题目区）。

        返回:
            list[dict]
        """
        try:
            return [json.loads(x) for x in open(self.path("chat.jsonl"), encoding="utf-8") if x.strip()]
        except OSError:
            return []

    def append(self, entry):
        """
        追加一条对话记录。

        参数:
            entry: dict
        """
        with open(self.path("chat.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def new_chat(self):
        """
        开始新对话：旧记录改名存档，清掉会话 ID。
        """
        if os.path.exists(self.path("chat.jsonl")):
            os.replace(self.path("chat.jsonl"), self.path(f"chat-{datetime.now():%Y%m%d-%H%M%S}.jsonl"))
        if os.path.exists(self.path("session.json")):
            os.remove(self.path("session.json"))


def tool_data(name, text):
    """
    从工具结果里取界面要用的部分（其余丢掉，保持数据流小）。

    参数:
        name: 工具名（不含 mcp__kakomon__ 前缀）
        text: 工具结果文本
    返回:
        dict 或 None
    """
    try:
        d = json.loads(text)
        if isinstance(d, dict) and list(d) == ["result"] and isinstance(d["result"], str):
            d = json.loads(d["result"])
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    if name == "similar_problems":
        return {"results": [{"problem_id": r["problem_id"], "title": r["title_zh"], "difficulty": r["difficulty"],
                             "source": f"{r['source']['university']} {r['source']['program']} · {r['source']['admission_year']} · "
                                       f"{r['source']['problem_label']}", "last_result": r.get("last_result", "")}
                            for r in d.get("results", [])[:6]]}
    if name == "record_attempt":
        return {"problem_id": d.get("problem_id", ""), "subquestions": d.get("subquestions", []),
                "mastery_changes": d.get("mastery_changes", [])[:3]}
    return d


def run_chat(app, handler, message, model):
    """
    运行一轮对话：调用 headless Claude Code，把流式事件翻译成界面事件（SSE）写回浏览器，结束后写入对话记录。

    参数:
        app: App
        handler: 当前请求的处理器（往它的 wfile 写事件）
        message: 学生这一轮说的话
        model: sonnet 或 opus
    """
    alive = [True]

    def send(ev):
        if not alive[0]:
            return
        try:
            handler.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            alive[0] = False

    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--model", model, "--mcp-config", app.mcp_config, "--strict-mcp-config", "--append-system-prompt", UI_NOTE,
           "--allowedTools", "mcp__kakomon", "Skill", "Read", "Bash(.venv/bin/python -I -c:*)"]
    if app.session():
        cmd += ["--resume", app.session()]
    app.append({"role": "user", "text": message, "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})
    started, texts, tools, views, current = time.time(), [], [], {}, None
    err_log = open(app.path("claude-stderr.log"), "w", encoding="utf-8")
    try:
        app.proc = subprocess.Popen(cmd, cwd=app.study, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err_log, text=True, bufsize=1)
        app.proc.stdin.write(message)
        app.proc.stdin.close()
    except FileNotFoundError:
        err_log.close()
        send({"t": "error", "message": "找不到 claude 命令：请先安装并登录 Claude Code"})
        return
    by_id, inputs, result = {}, {}, {}
    for line in app.proc.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("parent_tool_use_id"):
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            with open(app.path("session.json"), "w", encoding="utf-8") as f:
                json.dump({"id": ev.get("session_id", ""), "model": ev.get("model", "")}, f)
        elif kind == "stream_event":
            e = ev.get("event", {})
            if e.get("type") == "content_block_start":
                block = e.get("content_block", {})
                if block.get("type") == "text":
                    current = []
                    texts.append(current)
                    send({"t": "block"})
                elif block.get("type") == "tool_use":
                    name = block.get("name", "").replace("mcp__kakomon__", "")
                    if name != "ToolSearch":
                        by_id[block.get("id")] = name
                        tools.append(LABELS.get(name, name))
                        send({"t": "tool", "id": block.get("id"), "name": name, "label": LABELS.get(name, name)})
            elif e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta" and current is not None:
                current.append(e["delta"]["text"])
                send({"t": "text", "d": e["delta"]["text"]})
        elif kind == "assistant":
            for b in ev["message"].get("content", []):
                if b.get("type") == "tool_use" and b.get("id") in by_id:
                    inputs[b["id"]] = b.get("input", {})
                    send({"t": "input", "id": b["id"], "input": b.get("input", {})})
        elif kind == "user" and isinstance(ev.get("message", {}).get("content"), list):
            for b in ev["message"]["content"]:
                if b.get("type") != "tool_result" or b.get("tool_use_id") not in by_id:
                    continue
                name, c = by_id[b["tool_use_id"]], b.get("content")
                txt = c if isinstance(c, str) else "".join(x.get("text", "") for x in c if isinstance(x, dict))
                data = tool_data(name, txt) if name in KEEP_DATA and not b.get("is_error") else None
                if name == "exam_trends" and data:
                    data["input"] = inputs.get(b["tool_use_id"], {})
                if name == "similar_problems" and data:
                    data["for"] = inputs.get(b["tool_use_id"], {}).get("problem_id", "")
                if name == "record_attempt" and data:
                    data["problem_id"] = data.get("problem_id") or inputs.get(b["tool_use_id"], {}).get("problem_id", "")
                if name in ("exam_trends", "similar_problems") and data:
                    views["plan" if name == "exam_trends" else "similar"] = data
                send({"t": "tool_done", "id": b["tool_use_id"], "name": name, "ok": not b.get("is_error"), "data": data})
        elif kind == "result":
            result = ev
    app.proc.wait()
    err_log.close()
    err = open(app.path("claude-stderr.log"), encoding="utf-8").read().strip()
    app.proc = None
    text = "\n\n".join("".join(x) for x in texts if "".join(x).strip())
    seconds = round(time.time() - started)
    app.append({"role": "assistant", "text": text, "tools": tools, "views": views, "seconds": seconds,
                "cost": round(result.get("total_cost_usd", 0) or 0, 3)})
    if not result and not text:
        send({"t": "error", "message": (err or "Claude Code 没有返回结果")[-400:]})
    send({"t": "done", "seconds": seconds, "cost": round(result.get("total_cost_usd", 0) or 0, 3)})


def days_left(goal):
    """
    距考试天数。

    参数:
        goal: learner.active_goal 的结果
    返回:
        int 或 None
    """
    import learner
    day = learner.exam_day(goal["exam_date"]) if goal else None
    return (day - datetime.now().date()).days if day else None


def goal_view(app):
    """
    顶栏和概况页用的目标信息。

    参数:
        app: App
    返回:
        dict 或 None
    """
    import bank
    import learner
    g = learner.active_goal(app.learn)
    if not g:
        return None
    progs = sorted({p.split(" ", 1)[-1] for k in g["programs"] for u in g["universities"] for p in bank.matched_programs(app.bank, k, u)})
    return {"exam_date": g["exam_date"], "universities": g["universities"], "programs": g["programs"],
            "matched_programs": progs, "days": days_left(g)}


def state(app):
    """
    右栏与顶栏数据：目标、今日作答、掌握度（按目标学校出题频率）、倾向（学习数据摘要）、到期复习。

    参数:
        app: App
    返回:
        dict
    """
    import bank
    import insights
    import learner
    goal = goal_view(app)
    plan = insights.study_plan(app.bank, app.learn, top_n=6)
    freq = {}
    if goal:
        for c in bank.exam_trends(app.bank, "+".join(goal["universities"]), " ".join(goal["programs"]), top_n=60)["concepts"]:
            freq[c["concept_id"]] = c["problems"]
    today = datetime.now().strftime("%Y-%m-%d")
    done = [a for a in learner.all_attempts(app.learn) if a["ts"].startswith(today) and a["result"] != "跳过"]
    due = learner.due_cards(app.learn)
    payload = insights.profile_payload(app.bank, app.learn)
    return {"goal": goal, "today": {"subquestions": len(done), "problems": len({a["problem_id"] for a in done})},
            "mastery": [{"name": p["name"], "mastery": p["mastery"], "level": p["level"], "problems": freq.get(p["concept_id"])}
                        for p in plan["plan"]],
            "matched_target": plan["matched_target"], "notice": plan["notice"],
            "tendencies": [{"id": f["id"], "text": f["text"]} for f in payload["facts"][:4]],
            "due": {"count": len(due), "items": [due_item(app, d) for d in due[:4]]}}


def due_item(app, d):
    """
    一道到期复习题的显示信息。

    参数:
        app: App
        d: learner.due_cards 的一项
    返回:
        dict
    """
    import bank
    p = bank.problem_detail(app.bank, d["problem_id"]) or {"subquestions": [], "source": {}}
    labels = {s["key"]: s["label"] for s in p["subquestions"]}
    s = p["source"]
    return {"problem_id": d["problem_id"], "source": f"{s.get('university', '')} {s.get('admission_year', '')} {s.get('problem_label', '')}".strip(),
            "labels": [labels.get(k, k) for k in d["keys"]], "overdue_days": d["overdue_days"]}


def workspace(app):
    """
    题目区数据：MCP 服务写的面板文件（要显示的题目、解答），加上这些题最近 6 小时内记录的各小问结果（每个小问取最新一次）。

    参数:
        app: App
    返回:
        dict，含 items、note、updated_at、recorded（题目 ID -> {小问编号: 结果}）
    """
    import bank
    import learner
    try:
        ws = json.load(open(app.viewer_file, encoding="utf-8"))
    except (OSError, ValueError):
        return {"items": [], "recorded": {}}
    items = ws.get("items") or ([ws] if ws.get("problem_id") else [])
    since = (datetime.now() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    recorded = {}
    for it in items:
        rows = [a for a in learner.all_attempts(app.learn, since=since) if a["problem_id"] == it["problem_id"]]
        if rows:
            names = {s["key"]: s["label"] for s in (bank.problem_detail(app.bank, it["problem_id"]) or {"subquestions": []})["subquestions"]}
            recorded[it["problem_id"]] = {names.get(a["subq_key"], a["subq_key"]): a["result"] for a in rows}
    return dict(ws, items=items, recorded=recorded)


def overview(app, subject):
    """
    学习概况页数据：目标、常考点与掌握度、学习画像、本周作答、题型强弱、到期复习。

    参数:
        app: App
        subject: 常考点的科目（ID、中文名或科目组，如“数学”）
    返回:
        dict
    """
    import bank
    import insights
    import learner
    import mastery
    goal = goal_view(app)
    trends = None
    if goal:
        trends = bank.exam_trends(app.bank, "+".join(goal["universities"]), " ".join(goal["programs"]), [subject], 12)
        st = insights.state_at(app.learn)
        for c in trends["concepts"]:
            m = mastery.summary(st, c["concept_id"])
            c.update(mastery=m["mastery"], level=m["level"])
    rep = insights.report(app.bank, app.learn, 7)
    days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    per_day = Counter(a["ts"][:10] for a in learner.all_attempts(app.learn, since=days[0]) if a["result"] != "跳过")
    payload = insights.profile_payload(app.bank, app.learn)
    prog = insights.progress(app.bank, app.learn)
    return {"goal": goal, "trends": trends, "subject": subject,
            "profile": {"markdown": payload["profile"], "facts": payload["facts"], "observations": payload["observations"][-5:],
                        "last_update": payload["last_update"]},
            "week": {"days": [{"day": d[5:], "n": per_day.get(d, 0)} for d in days], "attempts": rep["attempts"],
                     "results": rep["results"], "hinted": rep["hinted"], "habits": rep["habits"]},
            "types": prog["by_type"], "due": [due_item(app, d) for d in learner.due_cards(app.learn)]}


class Handler(BaseHTTPRequestHandler):
    """
    请求处理：静态网页、数据接口、聊天中转。除首页和静态文件外都要校验令牌。
    """

    app = None

    def log_message(self, format, *args):
        """
        关闭默认访问日志。
        """
        return

    def allowed_host(self):
        """
        Host 头必须是本机地址（防 DNS 重绑定）。

        返回:
            bool
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def authorized(self):
        """
        数据接口的校验：本机 Host ＋ 本次启动的令牌。

        返回:
            bool
        """
        return self.allowed_host() and secrets.compare_digest(self.headers.get("X-Kakomon-Token", ""), self.app.token)

    def reply(self, code, body, ctype="application/json; charset=utf-8"):
        """
        发送一个完整响应。

        参数:
            code: 状态码
            body: bytes、str 或可序列化对象
            ctype: Content-Type
        """
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def static(self, rel, base):
        """
        发送 base 目录下的文件（不允许跳出目录）。

        参数:
            rel: 相对路径
            base: 根目录
        """
        path = os.path.realpath(os.path.join(base, rel))
        if not path.startswith(os.path.realpath(base) + os.sep) or not os.path.isfile(path):
            return self.reply(404, {"error": "not found"})
        types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml"}
        with open(path, "rb") as f:
            self.reply(200, f.read(), types.get(os.path.splitext(path)[1].lower(), "application/octet-stream"))

    def do_GET(self):
        """
        GET：首页、静态文件、原卷与插图、数据接口。
        """
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if not self.allowed_host():
            return self.reply(403, {"error": "forbidden"})
        if url.path in ("/", "/index.html"):
            html = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read().replace("__TOKEN__", self.app.token)
            return self.reply(200, html, "text/html; charset=utf-8")
        if url.path.startswith("/viewer"):
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if url.path.startswith("/static/"):
            return self.static(url.path[len("/static/"):], STATIC)
        if url.path.startswith("/data/full/") and url.path.lower().endswith((".jpg", ".jpeg", ".png")):
            return self.static(url.path[len("/data/"):], os.path.join(ROOT, "data"))
        if not self.authorized():
            return self.reply(403, {"error": "forbidden"})
        try:
            if url.path == "/api/state":
                return self.reply(200, state(self.app))
            if url.path == "/api/overview":
                return self.reply(200, overview(self.app, q.get("subject", "数学")))
            if url.path == "/api/history":
                return self.reply(200, {"entries": self.app.history(), "running": self.app.proc is not None})
            if url.path == "/api/workspace":
                return self.reply(200, workspace(self.app))
        except Exception as e:  # noqa: BLE001
            return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        """
        POST：发消息（流式返回）、停止、新对话。
        """
        url = urlparse(self.path)
        if not self.authorized():
            return self.reply(403, {"error": "forbidden"})
        size = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(size) or b"{}") if size else {}
        if url.path == "/api/chat":
            message = (body.get("message") or "").strip()
            if not message:
                return self.reply(400, {"error": "消息为空"})
            if not self.app.lock.acquire(blocking=False):
                return self.reply(409, {"error": "上一条消息还在处理"})
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                run_chat(self.app, self, message, "opus" if body.get("model") == "opus" else "sonnet")
            finally:
                self.app.lock.release()
            return
        if url.path == "/api/stop":
            if self.app.proc:
                self.app.proc.terminate()
            return self.reply(200, {"stopped": True})
        if url.path == "/api/new":
            if self.app.proc:
                return self.reply(409, {"error": "上一条消息还在处理"})
            self.app.new_chat()
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "not found"})


def main():
    """
    解析参数，启动本地服务。
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18770)
    ap.add_argument("--sandbox", default="")
    ap.add_argument("--study", default="")
    args = ap.parse_args()
    Handler.app = App(args.port, args.sandbox, args.study)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    print(f"kakomon 学习界面：http://localhost:{args.port}/" + ("（试用数据：" + args.sandbox + "）" if args.sandbox else ""), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
