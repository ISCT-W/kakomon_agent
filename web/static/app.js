/* kakomon 学习界面：对话（流式）、题目区（计划 / 题目 / 解答）、右栏（掌握度、倾向、复习）、学习概况页。 */
"use strict";

const TOKEN = window.KAKOMON_TOKEN;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const OK = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#2F54C8" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="flex:none"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
const SUBJECTS = [["数学", "数学"], ["la", "线性代数"], ["prob", "概率论"], ["calc", "微积分"], ["计算机", "计算机"]];
const SUBJECT_NAME = Object.fromEntries(SUBJECTS);
const S = { running: false, stick: true, plan: null, similar: null, simFor: "", progress: {}, pending: {}, tab: "题目", item: 0,
  scan: true, ws: { items: [] }, wsStamp: null, subject: "数学" };
const thread = $("#thread");

async function api(path, body) {
  const opts = { headers: { "X-Kakomon-Token": TOKEN } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  return fetch(path, opts);
}

let staleShown = false;
async function getJSON(path) {
  const r = await api(path);
  const data = await r.json().catch(() => ({}));
  if (r.status === 403 && !staleShown) {
    staleShown = true;
    const n = document.createElement("p");
    n.className = "note";
    n.textContent = "连接已失效（本地服务可能重启过），请刷新页面。";
    thread.appendChild(n);
  }
  if (!r.ok) throw new Error(data.error || "HTTP " + r.status);
  return data;
}

if (window.marked) {
  marked.use({ renderer: {
    html(html) { return esc(typeof html === "string" ? html : (html && html.text) || ""); },
    link(href, title, text) {
      if (href && typeof href === "object") ({ href, title, text } = href);
      return /^(https?:|#|\/)/i.test(href || "") ? '<a href="' + esc(href) + '" target="_blank" rel="noopener">' + text + "</a>" : text;
    },
    image(href, title, text) {
      if (href && typeof href === "object") ({ href, title, text } = href);
      return /^(https?:|\/)/i.test(href || "") ? '<img src="' + esc(href) + '" alt="' + esc(text) + '">' : esc(text);
    },
  } });
}

function renderMd(md) {
  if (!window.marked) return "<p>" + esc(md).replace(/\n/g, "<br>") + "</p>";
  const math = [];
  const prot = (md || "").replace(/\$\$([\s\S]+?)\$\$/g, (m, t) => { math.push([t, true]); return "@@M" + (math.length - 1) + "@@"; })
    .replace(/\$([^$\n]+?)\$/g, (m, t) => { math.push([t, false]); return "@@M" + (math.length - 1) + "@@"; });
  return marked.parse(prot).replace(/@@M(\d+)@@/g, (m, i) => {
    const [tex, disp] = math[+i];
    if (!window.katex) return esc(tex);
    try { return katex.renderToString(tex, { displayMode: disp, throwOnError: false, strict: false }); } catch (e) { return esc(tex); }
  });
}

function markLang(root) {
  root.querySelectorAll("p, li, td").forEach(e => {
    const t = e.textContent;
    const kana = (t.match(/[぀-ヿ]/g) || []).length;
    const cjk = (t.match(/[぀-ヿ一-鿿]/g) || []).length;
    if (kana >= 4 && kana / Math.max(1, cjk) >= 0.2) e.lang = "ja";
  });
}

/* ---------- 对话 ---------- */

function toBottom(force) { if (force || S.stick) thread.scrollTop = thread.scrollHeight; }
thread.addEventListener("scroll", () => { S.stick = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120; });

function hello() {
  thread.innerHTML = '<div class="hello"><b>你好！</b>可以这样开始：<ul>' +
    ["我考东科大计算机，2027 年 8 月考试，数学该怎么复习？", "出一道线性代数特征值的题，从简单的开始", "今天练什么？"]
      .map(x => '<li><a href="#/" data-say="' + esc(x) + '">' + esc(x) + "</a></li>").join("") + "</ul></div>";
}

function addUser(text) {
  const d = document.createElement("div");
  d.className = "ububble";
  d.textContent = text;
  thread.appendChild(d);
}

function addTurn() {
  const el = document.createElement("div");
  el.className = "aturn";
  el.innerHTML = '<div class="act" hidden></div><span class="who">助手</span><div class="md"></div>';
  thread.appendChild(el);
  return { act: el.querySelector(".act"), body: el.querySelector(".md"), text: "", tools: [], seconds: null, error: "", live: true };
}

function renderAct(turn) {
  const act = turn.act, tools = turn.tools;
  if (turn.error) {
    act.hidden = false;
    act.className = "act err";
    act.textContent = turn.error;
    return;
  }
  if (!tools.length) { act.hidden = true; return; }
  act.hidden = false;
  act.className = "act";
  const labels = [...new Set(tools.map(t => t.label))].join(" · ");
  const running = tools.find(t => !t.done);
  if (turn.live && running) {
    act.innerHTML = '<span class="spin" aria-hidden="true"></span><span>' + esc(running.label) + "</span><b>第 " + (tools.indexOf(running) + 1) + " 步</b>";
  } else if (turn.live) {
    act.innerHTML = '<span class="spin" aria-hidden="true"></span><span>' + esc(labels) + "</span><b>" + tools.length + " 步</b>";
  } else {
    act.innerHTML = OK + "<span>" + esc(labels) + "</span><b>" + tools.length + " 步" + (turn.seconds != null ? " · " + turn.seconds + " 秒" : "") + "</b>";
  }
}

function renderText(turn) {
  turn.body.innerHTML = renderMd(turn.text);
  turn.body.classList.toggle("typing", turn.live);
  markLang(turn.body);
  toBottom();
}

let textFrame = null;
function scheduleText(turn) {
  if (textFrame) return;
  textFrame = requestAnimationFrame(() => { textFrame = null; renderText(turn); });
}

function setBusy(busy) {
  $("#send").disabled = busy;
  $("#stop").hidden = !busy;
  $("#newChat").disabled = busy;
  document.querySelectorAll(".chips button").forEach(b => { b.disabled = busy; });
}

async function send(text) {
  text = (text || "").trim();
  if (!text || S.running) return;
  if (location.hash.startsWith("#/overview")) location.hash = "#/";
  S.running = true;
  setBusy(true);
  if (thread.querySelector(".hello")) thread.innerHTML = "";
  addUser(text);
  const turn = addTurn();
  S.stick = true;
  renderText(turn);
  toBottom(true);
  try {
    const resp = await api("/api/chat", { message: text, model: $("#model").value });
    if (!resp.ok) {
      turn.error = (await resp.json().catch(() => ({}))).error || "出错了（HTTP " + resp.status + "）";
    } else {
      const reader = resp.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, i);
          buf = buf.slice(i + 2);
          if (!chunk.startsWith("data: ")) continue;
          try { handle(turn, JSON.parse(chunk.slice(6))); } catch (err) { console.error("事件处理出错", err); }
        }
      }
    }
  } catch (e) {
    turn.error = "连接中断：" + e.message;
  }
  turn.live = false;
  if (!turn.text && !turn.error) turn.error = "这一轮没有回答（可能被停止了）";
  renderText(turn);
  renderAct(turn);
  S.running = false;
  setBusy(false);
  refreshState();
}

function handle(turn, ev) {
  if (ev.t === "block") {
    if (turn.text.trim()) turn.text += "\n\n";
  } else if (ev.t === "text") {
    turn.text += ev.d;
    scheduleText(turn);
  } else if (ev.t === "tool") {
    turn.tools.push({ id: ev.id, name: ev.name, label: ev.label, done: false });
    renderAct(turn);
  } else if (ev.t === "input") {
    const t = turn.tools.find(x => x.id === ev.id);
    if (t) t.input = ev.input;
  } else if (ev.t === "tool_done") {
    const t = turn.tools.find(x => x.id === ev.id);
    if (t) { t.done = true; t.ok = ev.ok; }
    renderAct(turn);
    onTool(ev);
  } else if (ev.t === "done") {
    turn.seconds = ev.seconds;
  } else if (ev.t === "error") {
    turn.error = ev.message;
    renderAct(turn);
  }
}

function onTool(ev) {
  const d = ev.data;
  if (ev.name === "exam_trends" && d) { S.plan = d; S.tab = "计划"; renderWork(); }
  if (ev.name === "similar_problems" && d) { S.similar = d.results; S.simFor = d.for || ""; renderWork(); }
  if (ev.name === "record_attempt" && d) {
    S.pending = {};
    refreshWork();
  }
  if (["show_problems", "serve_problem", "next_question", "save_solution"].includes(ev.name) && ev.ok) {
    refreshWork(ev.name === "save_solution" ? "解答" : "题目");
  }
  if (["set_goal", "record_attempt", "save_profile", "note_observation"].includes(ev.name)) refreshState();
}

async function loadHistory() {
  let h;
  try { h = await getJSON("/api/history"); } catch (e) { hello(); return; }
  if (!h.entries.length) { hello(); return; }
  for (const e of h.entries) {
    if (e.role === "user") { addUser(e.text); continue; }
    const turn = addTurn();
    turn.live = false;
    turn.text = e.text || "";
    turn.tools = (e.tools || []).map(label => ({ label, done: true }));
    turn.seconds = e.seconds;
    if (!turn.text) turn.error = "这一轮没有回答";
    renderText(turn);
    renderAct(turn);
    if (e.views && e.views.plan) S.plan = e.views.plan;
    if (e.views && e.views.similar) { S.similar = e.views.similar.results; S.simFor = e.views.similar.for || ""; }
  }
  if (h.running) {
    const n = document.createElement("p");
    n.className = "note";
    n.textContent = "上一条消息还在处理，完成后刷新页面查看。";
    thread.appendChild(n);
  }
  toBottom(true);
}

/* ---------- 题目区 ---------- */

function items() {
  const ws = S.ws || {};
  return ws.items || (ws.problem_id ? [ws] : []);
}

function srcLine(s) {
  return [s.university, s.graduate_school, s.admission_year ? s.admission_year + " 年度入学" : "", s.paper_title].filter(Boolean).join(" · ");
}

function titleOf(s) {
  const m = String(s.problem_label || "").match(/\d+/);
  return m ? "第 " + m[0] + " 題" : (s.problem_label || "题目");
}

function empty(title, text) {
  return '<div class="empty"><b>' + esc(title) + "</b><span>" + esc(text) + "</span></div>";
}

async function refreshWork(tab) {
  try {
    const ws = await getJSON("/api/workspace");
    if (ws.updated_at !== S.wsStamp) { S.item = 0; S.pending = {}; S.progress = {}; }
    for (const [pid, rec] of Object.entries(ws.recorded || {})) S.progress[pid] = Object.assign(S.progress[pid] || {}, rec);
    S.ws = ws;
    S.wsStamp = ws.updated_at;
    if (tab) S.tab = tab;
    renderWork();
  } catch (e) { /* 题目区暂时读不到时保持原样 */ }
}

function tabsHtml(extra) {
  return '<div class="tabs" role="tablist">' + ["计划", "题目", "解答"].map(t =>
    '<button type="button" role="tab" data-tab="' + t + '" aria-selected="' + (S.tab === t) + '">' + t + "</button>").join("") +
    '<div class="right">' + (extra || "") + "</div></div>";
}

function renderWork() {
  const w = $("#work"), list = items();
  if (!list.length && S.tab !== "计划") S.tab = S.plan ? "计划" : "题目";
  let head = "", body = "", foot = "";
  if (S.tab === "计划") {
    const p = S.plan;
    if (p && p.scope && p.concepts) {
      const subj = ((p.input && p.input.subjects) || []).map(x => SUBJECT_NAME[x] || x).join("、");
      head = '<div class="whead"><span class="kicker">备考计划 · ' + esc(p.scope.universities.concat(p.scope.programs).join(" ")) + "</span>" +
        '<div class="wtitle"><h1>' + esc((subj || "全部科目") + "常考点") + '</h1><span class="chip blue">' + esc(p.scope.years + " · " + p.scope.problems + " 道大题") +
        "</span></div>" + tabsHtml() + "</div>";
      body = '<table class="trends"><thead><tr><th>知识点</th><th>考过</th><th>出题年份</th><th>最近</th></tr></thead><tbody>' +
        p.concepts.map(c => "<tr><td>" + esc(c.name) + '</td><td class="n">' + c.problems + " 题</td><td>" + esc(c.years) + '</td><td class="n">' +
          esc(c.last || "") + "</td></tr>").join("") + "</tbody></table>";
      foot = "按考过的年数排序；统计范围是题库里该校该专攻的过去问";
    } else {
      head = '<div class="whead">' + tabsHtml() + "</div>";
      body = empty("还没有统计", "问助手“东科大常考什么”，常考点统计会显示在这里");
    }
  } else if (!list.length) {
    head = '<div class="whead">' + tabsHtml() + "</div>";
    body = empty("还没有题目", "告诉助手你想练什么，题目、原卷和解答会显示在这里");
  } else {
    S.item = Math.min(S.item, list.length - 1);
    const it = list[S.item], s = it.source || {}, focus = it.focus || [];
    const seg = list.length > 1 ? '<span class="seg">' + list.map((x, i) => '<button type="button" data-item="' + i + '" aria-pressed="' + (i === S.item) + '">' +
      "第 " + (i + 1) + " 题 · " + esc((x.source || {}).admission_year || "") + "</button>").join("") + "</span>" : "";
    const scan = S.tab === "题目" && (it.page_images || []).length ? '<button type="button" class="toggle" data-scan aria-pressed="' + S.scan + '"><i></i>原卷并排</button>' : "";
    const chips = ['<span class="chip">难度 ' + esc(it.difficulty) + "/5</span>", '<span class="chip">约 ' + esc(it.est_minutes) + " 分钟</span>"];
    if (S.ws.note) chips.push('<span class="chip blue">' + esc(S.ws.note) + "</span>");
    if (focus.length) chips.push('<span class="chip blue">本次 ' + esc(focus.length > 3 ? focus[0] + "–" + focus[focus.length - 1] : focus.join("、")) + "</span>");
    head = '<div class="whead"><span class="kicker">' + esc(srcLine(s)) + '</span><div class="wtitle"><h1>' + esc(titleOf(s)) + "</h1>" + chips.join("") + "</div>" +
      tabsHtml(seg + scan) + "</div>";
    if (S.tab === "题目") {
      const figs = (it.figures || []).map(f => '<figure><img src="/data/' + esc(f.url) + '" alt="' + esc(f.description) + '"><figcaption>' + esc(f.description) + "</figcaption></figure>").join("");
      const pages = S.scan ? (it.page_images || []).map(p => '<figure><img src="/data/' + esc(p) + '" alt="原卷扫描"><figcaption>原卷 · ' +
        esc((p.match(/p(\d+)\.jpg$/) || [])[1] ? "第 " + (+p.match(/p(\d+)\.jpg$/)[1]) + " 页" : "") + "</figcaption></figure>").join("") : "";
      body = (it.errata_md ? '<div class="errata md"><b>勘误（作答以此为准）</b>' + renderMd(it.errata_md) + "</div>" : "") +
        '<div class="prob"><div class="txt md" lang="ja">' + renderMd(it.content_md) + figs + "</div>" + (pages ? '<div style="flex:1 1 300px;max-width:480px;min-width:0;display:flex;flex-direction:column;gap:12px">' + pages + "</div>" : "") + "</div>";
    } else {
      const sol = it.solution;
      if (sol) {
        const ai = /AI/.test(sol.label || "");
        body = '<div class="solmeta"><span class="chip ' + (ai ? "warn" : "blue") + '">' + esc(sol.label) + "</span>" +
          (sol.source ? "<span>来源：" + esc(sol.source) + "</span>" : "") + (sol.covers && sol.covers !== "全部" ? "<span>覆盖小问：" + esc(sol.covers) + "</span>" : "") +
          (sol.verified ? "<span>核对：" + esc(sol.verified) + "</span>" : "") + '</div><div class="md">' + renderMd(sol.text) + "</div>";
      } else {
        body = '<div class="empty" style="min-height:200px"><b>还没有解答</b><span>题库里没有现成解答时，助手会写一份 AI 解答并保存，之后直接复用</span>' +
          '<button type="button" class="btn primary" data-say="' + esc("讲解一下这道题：" + srcLine(s) + " " + titleOf(s) + "（" + it.problem_id + "）") + '">请助手讲解</button></div>';
      }
      if (S.similar && S.similar.length && (!S.simFor || S.simFor === it.problem_id)) {
        body += '<div class="sim"><h3>相似题</h3>' + S.similar.map((r, i) => '<div class="row"><div class="l"><b>' + esc(r.source) + "</b><span>" + esc(r.title) +
          '</span></div><span class="note">难度 ' + esc(r.difficulty) + "/5" + (r.last_result ? " · 上次 " + esc(r.last_result) : "") + '</span><button type="button" class="btn' +
          (i === 0 ? " primary" : "") + '" data-say="' + esc("我想做这道题：" + r.source + "（" + r.problem_id + "）") + '">做这题</button></div>').join("") + "</div>";
      }
    }
    const rec = S.progress[it.problem_id] || {};
    if (focus.length) {
      foot = "<span>本题进度</span>" + focus.map(l => {
        if (rec[l]) return '<span class="sq ' + (rec[l] === "会" ? "done" : "bad") + '">' + esc(l) + " " + esc(rec[l]) + "</span>";
        return '<span class="sq">' + esc(l) + ["会", "部分会", "不会"].map(r => ' <button type="button" data-sq="' + esc(l) + '" data-r="' + r + '" aria-pressed="' +
          (S.pending[l] === r) + '">' + r + "</button>").join("") + "</span>";
      }).join("") + (focus.every(l => rec[l]) ? "" : '<button type="button" class="btn primary" data-submit ' +
        (Object.keys(S.pending).length ? "" : "disabled") + ">提交反馈</button>");
    } else if (Object.keys(rec).length) {
      foot = "<span>" + (it.solution ? "解答已保存 · " : "") + "本题进度</span>" + Object.entries(rec).map(([l, r]) =>
        '<span class="sq ' + (r === "会" ? "done" : "bad") + '">' + esc(l) + " " + esc(r) + "</span>").join("");
    } else if (it.solution) {
      foot = "<span>解答已保存，之后同一道题直接复用</span>";
    } else {
      foot = "<span>展示中，未出题</span>" + '<button type="button" class="btn" data-say="' + esc("出这道题给我做：" + srcLine(s) + " " + titleOf(s) + "（" + it.problem_id + "）") + '">做这道题</button>';
    }
  }
  w.innerHTML = head + '<div class="wbody">' + body + "</div>" + (foot ? '<div class="wfoot">' + foot + "</div>" : "");
  markLang(w);
}

$("#work").addEventListener("click", e => {
  const b = e.target.closest("button, a[data-say]");
  if (!b) return;
  if (b.dataset.tab) { S.tab = b.dataset.tab; renderWork(); }
  else if (b.dataset.item) { S.item = +b.dataset.item; renderWork(); }
  else if (b.hasAttribute("data-scan")) { S.scan = !S.scan; renderWork(); }
  else if (b.dataset.sq) { S.pending[b.dataset.sq] = S.pending[b.dataset.sq] === b.dataset.r ? undefined : b.dataset.r;
    if (!S.pending[b.dataset.sq]) delete S.pending[b.dataset.sq]; renderWork(); }
  else if (b.hasAttribute("data-submit")) {
    const msg = Object.entries(S.pending).map(([l, r]) => l + " " + r).join("，");
    if (msg) send(msg);
  } else if (b.dataset.say) { e.preventDefault(); send(b.dataset.say); }
});

/* ---------- 顶栏与右栏 ---------- */

function goalText(g) {
  const progs = (g.matched_programs && g.matched_programs.length ? g.matched_programs : g.programs).join("、");
  const [y, m, d] = g.exam_date.split("-");
  return g.universities.join("、") + (progs ? " " + progs : "") + " · " + y + " 年 " + (+m) + " 月" + (d ? " " + (+d) + " 日" : "") + "考试" +
    (g.days != null ? " · 还有 " + g.days + " 天" : "");
}

async function refreshState() {
  let st;
  try { st = await getJSON("/api/state"); } catch (e) { return; }
  const g = $("#goal");
  g.textContent = st.goal ? goalText(st.goal) : "还没有设定备考目标";
  g.className = "goal " + (st.goal ? "set" : "none");
  $("#today").textContent = "今日 " + st.today.subquestions + " 个小问";
  $("#dueTop").textContent = "到期复习 " + st.due.count;
  $("#mscope").textContent = st.goal ? "按目标学校出题频率" : "全部学校";
  $("#mastery").innerHTML = st.mastery.length ? st.mastery.map(m => {
    const touched = m.level !== "未接触";
    return '<div class="mrow"><div class="t"><span>' + esc(m.name.replace(/（.*?）/g, "")) + "</span><span>" + esc(touched ? m.mastery.toFixed(2) + " · " + m.level : "未接触") +
      (m.problems ? " · 考过 " + m.problems + " 题" : "") + '</span></div><div class="bar">' +
      (touched ? '<i class="' + (m.mastery < 0.4 ? "weak" : "") + '" style="width:' + Math.round(m.mastery * 100) + '%"></i>' : "") + "</div></div>";
  }).join("") + (st.notice ? '<p class="note">' + esc(st.notice.replace(/，请转告学生$/, "")) + "</p>" : "")
    : '<p class="note">还没有数据。告诉助手考试时间和目标学校，这里会按出题频率列出要练的知识点。</p>';
  $("#tend").innerHTML = st.tendencies.length ? st.tendencies.map(f => '<div class="fact">' + esc(f.text) + " <small>" + esc(f.id) + "</small></div>").join("")
    : '<p class="note">还没有足够的作答记录。做几道题、按小问反馈后，这里会出现你的错误习惯和偏好。</p>';
  $("#dueN").textContent = st.due.count + " 道";
  $("#due").innerHTML = st.due.count ? '<ul class="duelist">' + st.due.items.map(d => "<li><span>" + esc(d.source) + " " + esc(d.labels.join("")) +
    "</span><span>" + (d.overdue_days > 1 ? "逾期 " + Math.round(d.overdue_days) + " 天" : "今天") + "</span></li>").join("") +
    '</ul><button type="button" class="btn" data-say="开始复习到期的题">开始复习</button>'
    : '<p class="note">暂无。按小问记录作答后，会按遗忘曲线安排复习。</p>';
}

document.querySelector(".rail").addEventListener("click", e => {
  const b = e.target.closest("[data-say]");
  if (b) send(b.dataset.say);
});

/* ---------- 学习概况 ---------- */

async function loadOverview() {
  const page = $("#page-overview");
  let o;
  try { o = await getJSON("/api/overview?subject=" + encodeURIComponent(S.subject)); }
  catch (e) { page.innerHTML = '<section class="card"><p class="note">读取失败：' + esc(e.message) + "</p></section>"; return; }
  const g = o.goal;
  const goalCard = '<section class="card"><span class="kicker">备考目标</span>' + (g
    ? '<div style="display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 24px"><h1 style="margin:0;font-size:21px">' + esc(goalText(g).split(" · ")[0]) +
      '</h1><span class="big">' + esc(g.days ?? "—") + '</span><span>天后考试（' + esc(g.exam_date) + "）</span></div>" +
      '<p class="note">要修改目标，直接在对话里告诉助手新的考试时间或学校。</p>'
    : '<p class="note">还没有设定。在对话里告诉助手考试时间和目标学校，例如“我考东科大计算机，2027 年 8 月考试”。</p>') + "</section>";
  const subj = '<div class="subjects">' + SUBJECTS.map(([k, v]) => '<button type="button" data-subject="' + esc(k) + '" aria-pressed="' + (k === S.subject) + '">' +
    esc(v) + "</button>").join("") + "</div>";
  const trends = o.trends && o.trends.concepts.length
    ? '<div style="overflow-x:auto"><table class="trends"><thead><tr><th>知识点</th><th>考过</th><th>出题年份</th><th style="width:180px">我的掌握度</th></tr></thead><tbody>' +
      o.trends.concepts.map(c => {
        const touched = c.level !== "未接触";
        return "<tr><td>" + esc(c.name) + '</td><td class="n">' + c.problems + " 题</td><td>" + esc(c.years) + '</td><td><div style="display:flex;align-items:center;gap:8px">' +
          '<div class="bar" style="flex:1">' + (touched ? '<i class="' + (c.mastery < 0.4 ? "weak" : "") + '" style="width:' + Math.round(c.mastery * 100) + '%"></i>' : "") +
          '</div><span class="note" style="white-space:nowrap">' + esc(touched ? c.mastery.toFixed(2) : "未接触") + "</span></div></td></tr>";
      }).join("") + "</tbody></table></div><p class=\"note\">统计范围：" + esc(o.trends.scope.universities.concat(o.trends.scope.programs).join(" ")) + " " +
      esc(o.trends.scope.years) + " · " + o.trends.scope.problems + " 道大题</p>"
    : '<p class="note">' + (g ? "这个科目在目标学校的题库里没有题。" : "设定目标后，这里会列出目标学校的常考点和你的掌握度。") + "</p>";
  const prof = o.profile;
  const profile = (prof.markdown ? '<div class="md">' + renderMd(prof.markdown) + "</div>" : "") +
    (prof.facts.length ? (prof.markdown ? '<h3 style="margin:8px 0 0;font-size:14px">学习数据摘要</h3>' : "") +
      prof.facts.map(f => '<div class="fact">' + esc(f.text) + " <small>" + esc(f.id) + "</small></div>").join("") : "") +
    (!prof.markdown && !prof.facts.length ? '<p class="note">作答记录还不够。做几道题后，助手会根据作答数据和观察写出画像。</p>' : "") +
    (prof.last_update ? '<p class="note">画像更新于 ' + esc(prof.last_update) + "</p>" : "");
  const maxN = Math.max(1, ...o.week.days.map(d => d.n));
  const res = o.week.results || {};
  const total = Object.values(res).reduce((a, b) => a + b, 0);
  const week = '<div class="weekbars" role="img" aria-label="最近 7 天每天作答的小问数">' + o.week.days.map(d => '<div><i class="' + (d.n ? "" : "zero") +
    '" style="height:' + Math.max(3, Math.round(d.n / maxN * 84)) + 'px"></i><span>' + esc(d.day) + "</span></div>").join("") + "</div>" +
    '<p class="note">' + (total ? total + " 个小问 · 会 " + Math.round((res["会"] || 0) / total * 100) + "% · 看过提示 " + o.week.hinted + " 个" : "这 7 天还没有作答") + "</p>";
  const types = o.types.length ? o.types.map(t => '<div class="mrow"><div class="t"><span>' + esc(t.name) + "</span><span>" +
    esc((t.avg_score != null ? t.avg_score.toFixed(2) + " · " : "") + t.attempts + " 次" + (t.skips ? " · 跳过 " + t.skips : "")) + '</span></div><div class="bar"><i class="' +
    ((t.avg_score || 0) < 0.4 ? "weak" : "") + '" style="width:' + Math.round((t.avg_score || 0) * 100) + '%"></i></div></div>').join("")
    : '<p class="note">还没有作答记录。</p>';
  const due = o.due.length ? '<ul class="duelist">' + o.due.map(d => "<li><span>" + esc(d.source) + " " + esc(d.labels.join("")) + "</span><span>" +
    (d.overdue_days > 1 ? "逾期 " + Math.round(d.overdue_days) + " 天" : "今天") + "</span></li>").join("") +
    '</ul><button type="button" class="btn primary" data-say="开始复习到期的题">开始复习</button>' : '<p class="note">暂无到期复习。</p>';
  page.innerHTML = goalCard +
    '<div class="orow"><section class="card" style="flex:3 1 560px;min-width:0"><div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px">' +
    '<h2>常考点与我的掌握度</h2><div style="margin-left:auto">' + subj + "</div></div>" + trends + "</section>" +
    '<section class="card" style="flex:2 1 320px;min-width:0"><h2>学习画像</h2>' + profile + "</section></div>" +
    '<div class="orow"><section class="card" style="flex:1 1 280px;min-width:0"><h2>最近 7 天</h2>' + week + "</section>" +
    '<section class="card" style="flex:1 1 260px;min-width:0"><h2>题型</h2>' + types + "</section>" +
    '<section class="card" style="flex:1 1 260px;min-width:0"><h2>到期复习</h2>' + due + "</section></div>";
  markLang(page);
}

$("#page-overview").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.subject) { S.subject = b.dataset.subject; loadOverview(); }
  else if (b.dataset.say) send(b.dataset.say);
});

/* ---------- 路由与初始化 ---------- */

function route() {
  const ov = location.hash.startsWith("#/overview");
  $("#page-main").hidden = ov;
  $("#page-overview").hidden = !ov;
  document.querySelectorAll(".top nav a").forEach(a => a.classList.toggle("on", a.dataset.page === (ov ? "overview" : "main")));
  if (ov) loadOverview();
}

window.addEventListener("hashchange", route);
thread.addEventListener("click", e => {
  const a = e.target.closest("[data-say]");
  if (a) { e.preventDefault(); send(a.dataset.say); }
});
$("#composer").addEventListener("submit", e => {
  e.preventDefault();
  const v = $("#input").value;
  if (!v.trim() || S.running) return;
  $("#input").value = "";
  send(v);
});
$("#input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
});
document.querySelector(".chips").addEventListener("click", e => {
  const b = e.target.closest("[data-say]");
  if (b) send(b.dataset.say);
});
$("#stop").addEventListener("click", () => api("/api/stop", {}));
$("#newChat").addEventListener("click", async () => {
  if (S.running) return;
  const r = await api("/api/new", {});
  if (!r.ok) return;
  Object.assign(S, { plan: null, similar: null, simFor: "", progress: {}, pending: {}, tab: "题目" });
  hello();
  renderWork();
});
try { $("#model").value = localStorage.getItem("kakomon.model") || "sonnet"; } catch (e) { /* 浏览器不让存时用默认 */ }
$("#model").addEventListener("change", () => { try { localStorage.setItem("kakomon.model", $("#model").value); } catch (e) { /* 忽略 */ } });

(async function init() {
  route();
  await loadHistory();
  await refreshWork();
  refreshState();
  setInterval(() => { if (!S.running) refreshWork(); }, 10000);
})();
