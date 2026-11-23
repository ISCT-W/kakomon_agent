"""
对话回归检查：在临时学习目录里（setup_study.py 建，学习记录、画像、面板、解答存档都指向临时位置）用 headless Claude Code
跑一段固定的多轮对话，逐轮检查：回答是中文、没有工具报错、工具结果没有超长、Bash 只用来跑 Python 核对、该调用的工具调用了。
改了 skill 或工具之后跑一次。会花钱（Sonnet 约 $1，Opus 约 $2–3），运行前确认。

用法:
    python -I agent/dialogue_check.py [--model sonnet] [--out 目录]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TURNS = [
    {"prompt": "我考东科大计算机，2027 年 8 月考试，为此需要复习数学。你会怎么建议？", "expect": ["set_goal"]},
    {"prompt": "介绍一下东科大线性代数的常考点并且列举两道对应的题目", "expect": ["exam_trends"]},
    {"prompt": "给我解答一下第一题。并且，有什么类似的过去问？限定在七帝大+东科范围内",
     "expect": ["save_solution", "similar_problems"]},
]
LONG_ANSWER = 2500


def parse(lines):
    """
    解析一轮 stream-json 输出。

    参数:
        lines: 输出的各行
    返回:
        dict：session_id、calls（name、input、result、error）、answer、cost、seconds
    """
    out, by_id, texts = {"session_id": "", "calls": [], "cost": 0.0, "seconds": 0}, {}, []
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            out["session_id"] = ev.get("session_id", "")
        elif ev.get("type") == "assistant":
            for b in ev["message"]["content"]:
                if b["type"] == "tool_use":
                    by_id[b["id"]] = {"name": b["name"].replace("mcp__kakomon__", ""), "input": b["input"],
                                      "result": "", "error": False}
                    out["calls"].append(by_id[b["id"]])
                elif b["type"] == "text" and b["text"].strip():
                    texts.append(b["text"])
        elif ev.get("type") == "user" and isinstance(ev["message"]["content"], list):
            for b in ev["message"]["content"]:
                if b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                    c = b.get("content")
                    txt = c if isinstance(c, str) else "".join(x.get("text", "") for x in c if isinstance(x, dict))
                    try:
                        txt = json.loads(txt)["result"]
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass
                    by_id[b["tool_use_id"]].update(result=txt, error=bool(b.get("is_error")))
        elif ev.get("type") == "result":
            out["cost"], out["seconds"] = ev.get("total_cost_usd", 0.0), round(ev.get("duration_ms", 0) / 1000)
    out["answer"] = texts[-1] if texts else ""
    return out


def chinese_ratio(text):
    """
    回答里中日文字符占全部文字的比例（去掉公式、代码和链接后统计）。

    参数:
        text: 回答
    返回:
        float
    """
    t = re.sub(r"\$\$.*?\$\$|\$[^$\n]*\$|```.*?```|`[^`]*`|\(\S+?\)|https?://\S+", " ", text, flags=re.S)
    letters = [ch for ch in t if ch.isalpha()]
    cjk = [ch for ch in letters if re.match(r"[぀-ヿ一-鿿]", ch)]
    return len(cjk) / max(1, len(letters))


def check(turn, r):
    """
    检查一轮的结果。

    参数:
        turn: TURNS 里的一项
        r: parse 的结果
    返回:
        list[str]，发现的问题（空表示通过）
    """
    problems = []
    if chinese_ratio(r["answer"]) < 0.5:
        problems.append("回答不是中文")
    for c in r["calls"]:
        if c["error"]:
            problems.append(f"{c['name']} 报错：{c['result'][:80]}")
        if "exceeds maximum allowed tokens" in c["result"]:
            problems.append(f"{c['name']} 结果超长")
        if c["name"] == "Bash" and not c["input"].get("command", "").startswith(".venv/bin/python -I -c"):
            problems.append(f"Bash 用法不对：{c['input'].get('command', '')[:60]}")
    called = {c["name"] for c in r["calls"]}
    problems += [f"没有调用 {t}" for t in turn["expect"] if t not in called]
    for c in r["calls"]:
        if c["name"] == "set_goal" and not c["error"]:
            res = json.loads(c["result"])
            if res.get("universities") != ["東京工業大学"] or not str(res.get("exam_date", "")).startswith("2027-08"):
                problems.append(f"目标保存不对：{res}")
    return problems


def main():
    """
    建临时学习目录，逐轮运行对话并检查，打印汇总；有问题时以状态码 1 退出。
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    out = os.path.abspath(args.out) if args.out else tempfile.mkdtemp(prefix="kakomon_check_")
    study, data = os.path.join(out, "study"), os.path.join(out, "data")
    os.makedirs(data, exist_ok=True)
    subprocess.run([sys.executable, "-I", os.path.join(ROOT, "agent", "setup_study.py"), study], check=True,
                   stdout=subprocess.DEVNULL)
    cfg = os.path.join(out, "mcp.json")
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {"kakomon": {
            "type": "stdio", "command": os.path.join(study, ".venv", "bin", "python"),
            "args": ["-I", os.path.join(ROOT, "agent", "server.py")],
            "env": {"KAKOMON_LEARNER_DB": os.path.join(data, "learner.sqlite"),
                    "KAKOMON_PROFILE": os.path.join(data, "profile.md"),
                    "KAKOMON_VIEWER_DIR": os.path.join(data, "viewer"), "KAKOMON_VIEWER_PORT": "18782",
                    "KAKOMON_SOLUTIONS": os.path.join(data, "solutions")}}}}, f, ensure_ascii=False, indent=2)
    session, failed, total = "", False, 0.0
    for k, turn in enumerate(TURNS, 1):
        cmd = ["claude", "-p", turn["prompt"], "--output-format", "stream-json", "--verbose", "--model", args.model,
               "--mcp-config", cfg, "--strict-mcp-config",
               "--allowedTools", "mcp__kakomon", "Skill", "Read", "Bash(.venv/bin/python -I -c:*)"]
        if session:
            cmd += ["--resume", session]
        proc = subprocess.run(cmd, cwd=study, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        with open(os.path.join(out, f"turn{k}.jsonl"), "w", encoding="utf-8") as f:
            f.write(proc.stdout)
        r = parse(proc.stdout.splitlines())
        session = session or r["session_id"]
        problems = check(turn, r) if r["answer"] else [f"没有回答（{proc.stderr.strip()[:120]}）"]
        failed = failed or bool(problems)
        total += r["cost"]
        tools = [c["name"] for c in r["calls"] if c["name"] != "ToolSearch"]
        note = "（偏长）" if len(r["answer"]) > LONG_ANSWER else ""
        print(f"第 {k} 轮  {r['seconds']} 秒  ${r['cost']:.2f}  回答 {len(r['answer'])} 字{note}  "
              f"{'通过' if not problems else '有问题'}\n  工具：{'、'.join(tools)}")
        for p in problems:
            print(f"  - {p}")
    print(f"合计 ${total:.2f}；记录在 {out}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
