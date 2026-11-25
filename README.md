# kakomon_agent

**中文** | [English](README.en.md) | [日本語](README.ja.md)

日本大学院入试（情報系）过去问学习助手。用 Claude 把你手上的过去问（PDF、照片）整理成按知识点标注的题库，再在 [Claude Code](https://claude.com/claude-code) 里以 MCP 服务＋skill 的形式刷题。

> 本仓库只有代码，不含任何过去问或由过去问生成的数据（著作权属于各大学）。题库需要用你自己的过去问生成。

## 功能

- **出题**：一次一道真题，按知识点从易到难；新题出完自动扩展到相邻知识点；到期复习优先（FSRS）。
- **记录**：按小问记录会 / 部分会 / 不会、提示次数、用时；掌握度用 Elo 计算，含遗忘衰减。
- **找题**：某校常考点统计（考过的年份与题数）、相似题、按描述分层找题（代码召回，Claude 挑选）。
- **讲解**：优先用题库收录的解答；没有时由 Claude 解答并用 sympy / numpy 核对，存为 AI 解答复用（`data/solutions/<题目ID>.ai.md`）；人工解答写成 `<题目ID>.human.md` 即可，优先于 AI 解答。
- **题目面板**：在本机浏览器里渲染公式和插图，可同时显示几道题和解答。
- **目标与报告**：考试倒计时、学习计划、周报、学习画像。

## 结构

| 路径 | 内容 |
|---|---|
| `pipeline/` | 数据管线：逐页转写 → 拼题 → 挂接解答与勘误 → 打标签 → 入库（默认走 Message Batches API） |
| `agent/` | MCP 服务（23 个工具）与学习逻辑、测试 |
| `.claude/skills/kakomon-tutor/` | 刷题流程（skill） |
| `web/` | 本地网页界面：对话、题目区（题目 / 原卷 / 解答）、我的学习，另有学习概况页 |
| `data/taxonomy/` | 知识点表与别名（287 个知识点） |
| `data/viewer/` | 题目面板页面 |

## 使用

1. 安装依赖（Python 3.12）：

   ```bash
   python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
   ```

2. 把过去问放进 `kakomon/<文件夹>/`（PDF，或一份试卷一个照片文件夹）。学校、研究科、专攻按顶层文件夹名识别，对照表在 `pipeline/build_manifest.py` 的 `FOLDER_INFO`，换成自己的目录时改这里。
3. 在项目根目录建 `.env`，写一行 `claude-key=<你的 Anthropic API key>`。
4. 生成题库（会花钱：先 `estimate` 看预算，`submit` 用 `--budget` 设上限）：

   ```bash
   .venv/bin/python -I pipeline/build_manifest.py
   .venv/bin/python -I pipeline/normalize_manifest.py
   .venv/bin/python -I pipeline/run_full.py render --docs all
   .venv/bin/python -I pipeline/run_full.py estimate --docs all
   ```

   之后按 `transcribe` → `structure` → `tag` 的顺序，每个阶段依次 `submit --stage ... --docs all`、`wait`、`collect`，最后 `load` 入库、`qa` 出验收报告。各命令见 `pipeline/run_full.py` 开头的说明。参考花费：约 600 份试卷（约 7,000 页）全量处理约 $80（Sonnet，Batch API）。
5. 开始学习，两种方式任选：
   - **网页界面**：左边对话，中间是题目（公式渲染、原卷并排）和解答，右边是掌握度、学习倾向和到期复习，另有学习概况页。

     ```bash
     .venv/bin/python -I web/server.py
     ```

     打开 <http://localhost:18770/>。每条消息由本机的 Claude Code 在后台处理（需要已安装并登录），默认 Sonnet，可在对话框上方换成 Opus。
     第一次启动会自动建学习目录 `~/kakomon-study`。想先试用、不碰正式学习记录时加 `--sandbox ~/kakomon-try`。
   - **Claude Code 终端**：

     ```bash
     .venv/bin/python -I agent/setup_study.py ~/kakomon-study
     ```

     然后 `cd ~/kakomon-study && claude`，说“我想练习线性代数的特征值”之类即可。题目面板：<http://localhost:18766/viewer/>。

## 测试

- `agent/test_learner.py`：合成题库上的多日模拟，不需要真实数据。
- `agent/smoke_test.py`：经 MCP 协议调用全部工具，需要已生成的题库。
- `agent/dialogue_check.py`：用 headless Claude Code 跑三轮固定对话并检查回答，会花钱（Sonnet 约 $1）。

## License

[MIT](LICENSE)
