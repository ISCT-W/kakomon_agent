# kakomon_agent

日本大学院入试（情報系）过去问学习助手：把 `kakomon/` 下的过去问整理成按知识点标注的题库，并在 Claude Code 里以 MCP 服务 + skill 的形式提供“按知识点从易到难出题、按小问记录作答、讲解、长期画像”的学习功能。
仓库只公开代码；过去问和由它们生成的数据（`kakomon/`、`data/` 下除 `taxonomy/` 与 `viewer/index.html` 以外的内容）只在本地，已在 `.gitignore` 中排除，不要提交。

## 目录

- `kakomon/`：原始过去问（PDF 与照片，只在本地）。不要改动，删除文件一律用 `trash`（进废纸篓），不要用 `rm`。
- `pipeline/`：数据管线（Sonnet 5.5：转写 → 拼题 → 挂接 → 打标签 → 入库，默认走 Batch API）。
  - `build_manifest.py` + `normalize_manifest.py`：试卷清单（`data/manifest.csv`）；`find_duplicates.py` + `plan_cleanup.py`：新增试卷时查重、列出要移除的文件（只列清单，删除用 trash）。
  - `transcribe.py`：单页转写（prompt v3，含页面年度与科目）；`assemble.py`：拼题（大文档分块、稳定题目 ID、解答/勘误条目）；
    `link.py`：跨文档挂接解答与勘误、题目查重；`tagging.py`：打标签（知识点、题型、难度、解法要点、小问依赖）；`db.py`：题库表结构与整库重建。
  - `run_full.py`：主脚本（estimate / submit / wait / collect / load / qa / status），`batch.py`：Batch 提交与取回、预算上限；`qa_report.py`：验收报告。
- `agent/`：学习助手 MCP 服务（`server.py`，23 个工具，薄封装）。
  - `tutor.py`：出题（主知识点 → 次知识点 → 相邻知识点 → 回炉 → 提前复习）、指定出题、按小问记录作答。
  - `bank.py`：题库查询（知识点别名、紧凑知识点表、相邻知识点、分层找题 `find_problems`（按共同知识点计分，小问压缩成一行）、相似题 `similar_problems`、出题趋势 `exam_trends`、学校/专攻别名与学校组（七帝大）、科目组（数学）、按校汇总的题库概况）；`labels.py`：题号与小问编号规范化。
  - `solutions.py`：解答存档。AI 解答存为 `data/solutions/<题目 ID>.ai.md`（讲解后由 `save_solution` 保存，之后复用），人工解答放 `<题目 ID>.human.md`（直接写文件即可）；取用优先级：题库收录 > 人工 > AI。
  - `learner.py`：学习记录（每个小问一条作答，含提示次数、用时、自报结果、跳过原因；复习卡片；目标、期间；观察记录；画像版本；报错）。
  - `mastery.py`：掌握度（Elo 重放；提示折扣、个性化自报折扣、遗忘衰减）；`review.py`：复习排期（FSRS）；`selector.py`：分层选题。
  - `insights.py`：掌握情况、题型强弱、回避倾向、学习习惯、预测校准、学习数据摘要（画像证据）、学习计划、周期报告。
  - `viewer.py` 题目面板（一次可显示几道题，可附解答）；`report.py` Markdown 周报；`issues.py` 报错维护（list / resolve）。
  - `setup_study.py`：建学习用目录（放在仓库外；只含 MCP 配置、技能链接、简短说明，默认 Sonnet），日常刷题在那里运行 `claude`。
  - `dialogue_check.py`：对话回归检查（临时学习目录里用 headless Claude Code 跑固定的三轮对话，检查中文回答、工具报错与超长、Bash 用法、新工具是否调用）。会花钱，运行前确认。
  - 测试：`test_learner.py`（合成题库＋固定时间戳的两天模拟，逐项断言）、`smoke_test.py`（经 MCP 协议调用全部工具，用正式题库）。
    测试务必用环境变量把数据指向临时位置：`KAKOMON_LEARNER_DB`、`KAKOMON_PROFILE`、`KAKOMON_VIEWER_DIR`、`KAKOMON_VIEWER_PORT`、`KAKOMON_SOLUTIONS`。
- `web/`：本地网页界面。`server.py` 是本地服务：只监听 127.0.0.1，每次启动生成令牌并校验 Host；每条消息在学习目录里调用一次 headless Claude Code
  （`--include-partial-messages` 流式、`--resume` 接续会话、`--append-system-prompt` 说明界面布局），工具结果驱动题目区和右栏；数据接口直接读题库和学习记录。
  `static/` 是页面（对话｜题目区｜我的学习，学习概况）。启动：`.venv/bin/python -I web/server.py`，打开 http://localhost:18770/；测试用 `--sandbox 目录`（对话会真实调用 Claude Code，花钱）。
- `data/`：清单、排除名单 `exclude.tsv`、知识点表与别名（`taxonomy/taxonomy_v1.yaml`、`aliases_v1.yaml`，287 个知识点；只追加不改名）、管线缓存 `full/`（`docs.txt` 本次处理的文档、`pages/` 转写、
  `structure/` 拼题、`problems/` 标签、`batch_state.json` 批次与费用、`load_report.json`、`qa/` 验收报告）、题库 `bank.sqlite`、
  学习记录 `learner.sqlite`、画像 `learner_profile.md`、解答存档 `solutions/`、题目面板 `viewer/`。
- `.mcp.json`：注册 `kakomon` MCP 服务；`.claude/skills/kakomon-tutor/`：刷题流程。

## 约定

- 代码简洁，不用表情符号；函数写 `"""` 说明（用途、参数、返回）。给学生的回答同样要简洁：先给结论，只写必要的步骤和依据。
- 依赖见 `requirements.txt`（`.venv/bin/python -m pip install -r requirements.txt`）。
- 运行脚本用项目虚拟环境并加 `-I`：`.venv/bin/python -I pipeline/xxx.py`（脚本自己把所在目录加入 `sys.path`）。
- 解释器约定：`.venv` 可以是真正的虚拟环境，也可以是指向已有环境的软链接（如 `ln -s ~/miniconda3/envs/dev .venv`），已被 gitignore，不要提交。`.mcp.json` 用相对路径 `${KAKOMON_PYTHON:-.venv/bin/python} -I agent/server.py` 从项目根目录启动服务；不想建 `.venv` 时设环境变量 `KAKOMON_PYTHON` 指向解释器。
- API 密钥在 `.env` 的 `claude-key`（变量名带连字符，由 `pipeline/common.py` 解析）；请求参数统一由 `common.request_params` 组装（同步与 Batch 一致），同步调用走 `common.call_params`。
- 花钱的步骤（转写、拼题、打标签、对话回归检查）必须先得到用户同意；`run_full.py submit` 用 `--budget` 设上限。
- 管线流程：`submit --stage transcribe` → `wait` → `collect` → `submit --stage structure` → `wait` → `collect` → `submit --stage tag` → `wait` → `collect` → `load` → `qa`。
  Batch 里失败或被拒的少数请求用 `submit --stage ... --sync` 补跑。缓存可断点续跑；题库随处理进度累积，`load` 不调用 API，新机器上可直接用缓存重建题库。
- 重复题：入库时按正文相似度（≥0.8）把同一道题并成组，保留最早的正式试卷题，其余标 `duplicate_of`，出题、找题和统计都跳过（`also_in` 列出它还出现在哪里）。
- 题目 ID = 文档 ID ＋ 规范化题号（合订本再加年度）；学习记录引用的题目或小问在重建题库时消失会停止入库（`load --force` 才替换）。
- MCP 服务走 stdio，服务代码不能向标准输出打印任何内容。工具结果要控制长度（找题等列表结果不超过约 3 万字），太长时 Claude Code 会把结果存成文件而不直接给模型。
- 题目面板：MCP 服务启动时在 127.0.0.1:18766 提供 `data/`，面板地址 `http://localhost:18766/viewer/`；网页界面默认 18770，它启动的 MCP 服务把面板端口设成同一个端口（不另起面板）。

## TODO

- [ ] **讲解范例**：解答的保存与复用已完成（`solutions.py`）；还差按用户提供的范例写讲解。
  - 范例放在 `.claude/skills/kakomon-tutor/examples/`（每个一份 Markdown，覆盖计算、证明、程序等题型），SKILL.md 第 5 节讲解前先读与当前题型最接近的一两个。
- [ ] **相似题第二版**：第一版 `similar_problems` 已完成（共同知识点、同题型、难度接近，排除原题和已掌握的题）。
  - 用打标签时已有的“解法要点”（`subquestions.method_zh`）加小问摘要做向量嵌入（本地多语言模型，如 bge-m3），在第一版的候选里重排。不对原文直接嵌入，LaTeX 噪声太大。
  - 先人工标注一批“应当相似”的小问对，检查前 5 条的命中率。
  - 学生答“不会”或“部分会”讲解完后，可以出一道相似题巩固。
- [ ] **其余**：整卷模考、复习积压提醒、学习记录按日期备份。
