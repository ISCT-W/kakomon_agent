"""
验收报告：根据题库、入库报告、批次状态和页面转写缓存，生成 data/full/qa/index.html 与 tag_check.csv。

index.html：概览（数量、费用、批次耗时）、问题清单、50 个抽检小问、每份文档的题目详情（公式渲染、原卷页面）。
tag_check.csv：50 个抽检小问的标签，留出“知识点是否合理”“难度是否合理”两栏给人工填写。
"""
import csv
import json
import os
import random
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime

SAMPLE_SIZE = 50
MAX_ISSUE_ITEMS = 200

PAGE_TEMPLATE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>数据管线验收报告</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<style>
:root { --bg:#f7f7f5; --card:#fff; --fg:#1d1d1f; --muted:#6b6b6b; --line:#e2e2de; --accent:#b4235a; --warn:#a15c00; }
@media (prefers-color-scheme: dark) { :root { --bg:#151515; --card:#1f1f1f; --fg:#ececec; --muted:#9a9a9a; --line:#333; --accent:#ff6b9a; --warn:#f0a040; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.7 -apple-system, "Hiragino Sans", "PingFang SC", sans-serif; }
main { max-width:1100px; margin:auto; padding:16px; }
h1 { font-size:22px; } h2 { font-size:18px; margin-top:28px; } h3 { font-size:16px; margin:6px 0; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 16px; margin:10px 0; overflow-x:auto; }
.muted { color:var(--muted); font-size:13px; } .warn { color:var(--warn); }
table { border-collapse:collapse; width:100%; font-size:13px; } td, th { border:1px solid var(--line); padding:3px 7px; text-align:left; vertical-align:top; }
img { max-width:100%; border:1px solid var(--line); background:#fff; border-radius:4px; }
.thumbs img { width:160px; margin:4px; vertical-align:top; }
details > summary { cursor:pointer; } .katex-display { overflow-x:auto; overflow-y:hidden; }
pre { white-space:pre-wrap; }
</style></head><body><main id="app"></main>
<script>
const DATA = __DATA__;
const PROBS = {};
function esc(s) { return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;"); }
function md(text) {
  const math = [];
  const prot = (text || "").replace(/\\$\\$([\\s\\S]+?)\\$\\$/g, (m, t) => { math.push([t, true]); return "@@M" + (math.length - 1) + "@@"; })
    .replace(/\\$([^$\\n]+?)\\$/g, (m, t) => { math.push([t, false]); return "@@M" + (math.length - 1) + "@@"; });
  return marked.parse(prot).replace(/@@M(\\d+)@@/g, (m, i) => {
    const [tex, disp] = math[+i];
    try { return katex.renderToString(tex, { displayMode: disp, throwOnError: false }); } catch (e) { return esc(tex); }
  });
}
function table(head, rows) {
  return "<table><tr>" + head.map(h => "<th>" + esc(h) + "</th>").join("") + "</tr>" +
    rows.map(r => "<tr>" + r.map(c => "<td>" + c + "</td>").join("") + "</tr>").join("") + "</table>";
}
function render() {
  const d = DATA, out = [];
  out.push("<h1>数据管线验收报告</h1><div class='muted'>生成于 " + esc(d.generated) + "</div>");
  out.push("<h2>一、概览</h2><div class='card'>" + table(["项目", "数值"], d.overview.map(x => [esc(x[0]), esc(x[1])])) + "</div>");
  out.push("<h2>二、批次（Batch API）</h2><div class='card'>" + table(["批次", "阶段", "请求数", "提交（UTC）", "结束（UTC）", "耗时（分钟）", "费用"],
    d.batches.map(b => [esc(b.id), esc(b.stage), esc(b.requests), esc(b.created), esc(b.ended), esc(b.minutes), "$" + esc(b.cost)])) + "</div>");
  out.push("<h2>三、请你抽检的 50 个小问</h2><div class='card'><p>同样的内容在 <code>tag_check.csv</code>，可以直接在表格里填写判定。点题号跳到下方题目详情。</p>" +
    table(["#", "题目", "小问", "要求（摘要）", "解法要点", "主知识点", "次知识点", "题型", "难度"],
      d.sample.map(s => [s.no, "<a href='#" + esc(s.problem_id) + "'>" + esc(s.problem_id) + "</a><div class='muted'>" + esc(s.source) + "</div>",
        esc(s.label), esc(s.summary_zh), esc(s.method_zh), esc(s.primary), esc(s.secondary), esc(s.question_type), esc(s.difficulty)])) + "</div>");
  out.push("<h2>四、问题清单</h2>");
  d.issues.forEach(g => out.push("<div class='card'><h3 class='" + (g.total ? "warn" : "") + "'>" + esc(g.title) + "（" + g.total + "）</h3>" +
    (g.items.length ? "<ul>" + g.items.map(i => "<li>" + esc(i) + "</li>").join("") + "</ul>" +
      (g.total > g.items.length ? "<div class='muted'>只显示前 " + g.items.length + " 条</div>" : "") : "<div class='muted'>无</div>") + "</div>"));
  out.push("<h2>五、每份文档的题目</h2>");
  d.docs.forEach(doc => {
    out.push("<div class='card'><h3>" + esc(doc.rel_path) + "</h3><div class='muted'>" + esc(doc.desc) + "</div>" +
      table(["题目 ID", "题号", "科目", "年度（依据）", "页", "小问", "解答", "勘误", "重复"], doc.problems.map(p => [
        "<a href='#" + esc(p.problem_id) + "'>" + esc(p.problem_id) + "</a>", esc(p.label), esc(p.section),
        esc(p.admission_year + "（" + p.year_basis + "）"), esc(p.pages.join(",")), esc(p.subqs.length),
        esc(p.solution_source ? "有：" + p.solution_source : ""), p.errata_md ? "有" : "",
        p.duplicate_of ? "与 <a href='#" + esc(p.duplicate_of) + "'>" + esc(p.duplicate_of) + "</a> 重复，出题时跳过" : ""])) + "</div>");
    doc.problems.forEach(p => {
      PROBS[p.problem_id] = p;
      out.push("<details class='card prob' id='" + esc(p.problem_id) + "'><summary><b>" + esc(p.problem_id) + "</b> " + esc(p.label) +
        " · " + esc(p.title_zh) + " · 难度 " + esc(p.difficulty) + " · 预计 " + esc(p.est_minutes) + " 分钟</summary><div class='body'></div></details>");
    });
  });
  document.getElementById("app").innerHTML = out.join("");
  document.querySelectorAll("details.prob").forEach(d => d.addEventListener("toggle", () => { if (d.open) fill(d); }));
  if (location.hash) { const d = document.getElementById(decodeURIComponent(location.hash.slice(1))); if (d) { d.open = true; d.scrollIntoView(); } }
}
function fill(d) {
  const body = d.querySelector(".body");
  if (body.dataset.done) return;
  const p = PROBS[d.id];
  body.innerHTML = (p.errata_md ? "<div class='card'><b>勘误</b>" + md(p.errata_md) + "</div>" : "") +
    "<div>" + md(p.content_md) + "</div>" +
    p.figures.map(f => "<figure><img loading='lazy' src='" + esc(f.url) + "'><figcaption class='muted'>" + esc(f.description) + "</figcaption></figure>").join("") +
    table(["小问", "要求", "解法要点", "知识点", "题型", "难度", "依赖"], p.subqs.map(s => [esc(s.label), esc(s.summary_zh), esc(s.method_zh),
      esc(s.concepts), esc(s.question_type), esc(s.difficulty), esc(s.depends_on)])) +
    (p.solution_md ? "<details><summary>解答（" + esc(p.solution_source) + "）</summary>" + md(p.solution_md) + "</details>" : "") +
    "<details><summary>原卷页面</summary><div class='thumbs'>" + p.page_images.map(u => "<a href='" + esc(u) + "' target='_blank'><img loading='lazy' src='" + esc(u) + "'></a>").join("") + "</div></details>";
  body.dataset.done = "1";
}
window.addEventListener("hashchange", () => { const d = document.getElementById(decodeURIComponent(location.hash.slice(1))); if (d) { d.open = true; d.scrollIntoView(); } });
render();
</script></body></html>
"""


def annotated(csv_path):
    """
    抽检表里是否已经有人工填写的判定（有则不能覆盖）。

    参数:
        csv_path: 抽检表路径
    返回:
        bool
    """
    if not os.path.exists(csv_path):
        return False
    with open(csv_path, encoding="utf-8-sig") as f:
        return any(any(c.strip() for c in row[10:]) for row in list(csv.reader(f))[1:])


def minutes_between(a, b):
    """
    两个 ISO 时间之间的分钟数。

    参数:
        a: 开始时间
        b: 结束时间
    返回:
        float 或空字符串
    """
    try:
        return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60, 1)
    except (TypeError, ValueError):
        return ""


def page_notes(pages_dir, doc_id):
    """
    一份文档各页的页面类型、拿不准之处与订正说明。

    参数:
        pages_dir: 页面缓存目录
        doc_id: 文档 ID
    返回:
        (Counter 页面类型, list[str] 拿不准之处, list[str] 订正说明, int 页数)
    """
    types, uncertain, corrections, n = Counter(), [], [], 0
    d = os.path.join(pages_dir, doc_id)
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not re.fullmatch(r"p\d{3}\.json", f):
            continue
        rec = json.load(open(os.path.join(d, f), encoding="utf-8"))
        res = rec.get("result") or {}
        n += 1
        types[res.get("page_type", "?")] += 1
        uncertain += [f"{f[:-5]}：{u}" for u in res.get("uncertain_spots", [])]
        if res.get("corrections_note"):
            corrections.append(f"{f[:-5]}：{res['corrections_note']}")
    return types, uncertain, corrections, n


def build(qa_dir, bank_path, load_report_path, state_path, pages_dir):
    """
    生成验收报告。

    参数:
        qa_dir: 输出目录
        bank_path: 题库路径
        load_report_path: 入库报告路径
        state_path: 批次状态路径
        pages_dir: 页面缓存目录
    返回:
        str，报告路径说明
    """
    os.makedirs(qa_dir, exist_ok=True)
    con = sqlite3.connect(bank_path)
    con.row_factory = sqlite3.Row
    rep = json.load(open(load_report_path, encoding="utf-8"))
    state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {"batches": {}, "spent": {}, "retry": {}}
    names = {r["concept_id"]: r["name"] for r in con.execute("SELECT concept_id, name FROM concepts")}
    rel_from_qa = os.path.relpath(os.path.dirname(os.path.dirname(qa_dir)), qa_dir)

    docs, all_subqs, type_total = [], [], Counter()
    uncertain_all, corrections_all = [], []
    for d in con.execute("SELECT * FROM documents ORDER BY rel_path"):
        types, uncertain, corrections, n_pages = page_notes(pages_dir, d["doc_id"])
        type_total.update(types)
        uncertain_all += [f"{d['rel_path']} {u}" for u in uncertain]
        corrections_all += [f"{d['rel_path']} {c}" for c in corrections]
        problems = []
        for p in con.execute("SELECT * FROM problems WHERE doc_id = ? ORDER BY seq", (d["doc_id"],)):
            subqs = []
            for s in con.execute("SELECT * FROM subquestions WHERE problem_id = ? ORDER BY seq", (p["problem_id"],)):
                cs = con.execute("SELECT concept_id, role FROM subq_concepts WHERE subq_id = ? ORDER BY role DESC",
                                 (s["subq_id"],)).fetchall()
                prim = [c["concept_id"] for c in cs if c["role"] == "primary"]
                sec = [c["concept_id"] for c in cs if c["role"] == "secondary"]
                row = {"label": s["label"], "summary_zh": s["summary_zh"], "method_zh": s["method_zh"],
                       "question_type": s["question_type"], "difficulty": s["difficulty"],
                       "depends_on": "、".join(json.loads(s["depends_on"] or "[]")),
                       "concepts": "；".join(f"{c}（{names.get(c, '')}）" for c in prim + sec),
                       "primary": "；".join(f"{c} {names.get(c, '')}" for c in prim),
                       "secondary": "；".join(f"{c} {names.get(c, '')}" for c in sec)}
                subqs.append(row)
                all_subqs.append(dict(row, problem_id=p["problem_id"], doc=d["rel_path"],
                                      source=f"{d['university']} {d['program']} {p['admission_year']} {p['label']}"))
            figs = [{"url": os.path.join(rel_from_qa, f["path"]).replace(os.sep, "/"), "description": f["description"]}
                    for f in con.execute("SELECT * FROM figures WHERE problem_id = ? ORDER BY seq", (p["problem_id"],))
                    if f["path"]]
            problems.append({
                "problem_id": p["problem_id"], "label": p["label"], "section": p["section"],
                "admission_year": p["admission_year"], "year_basis": p["year_basis"], "pages": json.loads(p["pages"]),
                "title_zh": p["title_zh"], "difficulty": p["difficulty"], "est_minutes": p["est_minutes"],
                "content_md": p["content_md"], "solution_md": p["solution_md"], "solution_source": p["solution_source"],
                "errata_md": p["errata_md"], "duplicate_of": p["duplicate_of"] or "", "subqs": subqs, "figures": figs,
                "page_images": [f"../pages/{d['doc_id']}/p{n:03d}.jpg" for n in json.loads(p["pages"])],
            })
        docs.append({"rel_path": d["rel_path"], "problems": problems,
                     "desc": f"{d['university']} {d['program']}｜{d['admission_year']} 年度｜{d['document_kind']}｜"
                             f"{d['languages']}｜有内容页 {n_pages}：" + "，".join(f"{k} {v}" for k, v in types.items())})

    rng = random.Random(0)
    by_doc = defaultdict(list)
    for s in all_subqs:
        by_doc[s["doc"]].append(s)
    for v in by_doc.values():
        rng.shuffle(v)
    sample, order = [], sorted(by_doc)
    while len(sample) < min(SAMPLE_SIZE, len(all_subqs)):
        for doc in order:
            if by_doc[doc] and len(sample) < SAMPLE_SIZE:
                sample.append(by_doc[doc].pop())
    for i, s in enumerate(sample, 1):
        s["no"] = i
    csv_path = os.path.join(qa_dir, "tag_check.csv")
    if annotated(csv_path):
        csv_path = os.path.join(qa_dir, f"tag_check_{datetime.now():%Y%m%d_%H%M}.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "题目ID", "出处", "小问", "要求（摘要）", "解法要点", "主知识点", "次知识点", "题型", "难度",
                    "知识点是否合理（Y/N）", "难度是否合理（Y/偏高/偏低）", "备注"])
        for s in sample:
            w.writerow([s["no"], s["problem_id"], s["source"], s["label"], s["summary_zh"], s["method_zh"], s["primary"],
                        s["secondary"], s["question_type"], s["difficulty"], "", "", ""])

    batches = []
    for bid, b in state["batches"].items():
        batches.append({"id": bid, "stage": b["stage"], "requests": len(b["items"]), "created": b["created"],
                        "ended": b.get("ended", ""), "minutes": minutes_between(b["created"], b.get("ended", "")),
                        "cost": b.get("actual_cost", "")})
    spent = Counter()
    for k, v in state["spent"].items():
        stage = next((b["stage"] for bid, b in state["batches"].items() if bid == k), "同步补跑")
        spent[stage] += v
    stats, lk = rep["stats"], rep["link"]
    overview = [
        ("文档", rep["docs"]), ("有内容页", sum(type_total.values())),
        ("页面类型", "，".join(f"{k} {v}" for k, v in type_total.most_common())),
        ("大题", stats["problems"]), ("小问", stats["subquestions"]),
        ("解答挂接（解答集条目）", f"{len(lk['solutions_linked'])} / {len(lk['solutions_linked']) + len(lk['solutions_unlinked'])}"),
        ("重述题面并入原卷", len(lk["restated_merged"])), ("解答集中保留为新题", len(lk["restated_kept"])),
        ("勘误挂接", f"{len(lk['errata_linked'])} / {len(lk['errata_linked']) + len(lk['errata_unlinked'])}"),
        ("疑似重复", f"{len(rep['near_duplicates'])} 对，标为重复题 {len(rep.get('duplicates', {}))} 道（出题与找题时跳过）"),
        ("费用合计", f"${sum(spent.values()):.3f}（" + "，".join(f"{k} ${v:.3f}" for k, v in spent.items()) + "）"),
        ("待补跑请求", len(state.get("retry", {}))),
    ]
    issues = [
        {"title": "拼题检查（片段未归属/重复）", "items": [f"{k}：{v}" for k, v in rep["structure_issues"].items()]},
        {"title": "未归入大题的片段", "items": [f"{k} {u['segment_id']}：{u['reason']}" for k, v in rep["unassigned"].items() for u in v]},
        {"title": "年度冲突", "items": [f"{k}：{c}" for k, v in rep["conflicts"].items() for c in v]},
        {"title": "未挂接的解答", "items": [f"{x['doc']} {x['year']} {x['label']}：{x['reason']}" for x in lk["solutions_unlinked"]]},
        {"title": "解答集中没找到原卷、保留为新题的题面", "items": [f"{x['doc']} {x['year']} {x['label']}：{x['reason']}" for x in lk["restated_kept"]]},
        {"title": "未挂接的勘误", "items": [f"{x['doc']} {x['label']}：{x['reason']}" for x in lk["errata_unlinked"]]},
        {"title": "缺标签未入库的题", "items": rep["untagged"]},
        {"title": "被剔除的无效知识点 ID", "items": [f"{k}：{v}" for k, v in rep["invalid_concepts"].items()]},
        {"title": "疑似重复的大题", "items": [f"{x['a']} ≈ {x['b']}（{x['similarity']}）" for x in rep["near_duplicates"]]},
        {"title": "待补跑的请求", "items": [f"{k}：{v['error']}" for k, v in state.get("retry", {}).items()]},
        {"title": "转写时拿不准的地方", "items": uncertain_all},
        {"title": "官方订正（已按订正后转写）", "items": corrections_all},
    ]
    for g in issues:
        g["total"] = len(g["items"])
        g["items"] = g["items"][:MAX_ISSUE_ITEMS]
    data = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "overview": [list(x) for x in overview],
            "batches": batches, "sample": sample, "issues": issues, "docs": docs}
    html_path = os.path.join(qa_dir, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(PAGE_TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    return f"验收报告：{html_path}\n抽检表：{csv_path}"
