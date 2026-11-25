# kakomon_agent

[中文](README.md) | **English** | [日本語](README.ja.md)

A study assistant for Japanese graduate school entrance exams (information science). It uses Claude to turn your own past exam papers (PDFs, photos) into a question bank tagged by topic, then lets you practice inside [Claude Code](https://claude.com/claude-code) through an MCP server and a skill.

> This repository contains code only. It includes no past exam papers and no data derived from them (copyright belongs to each university). You build the question bank from your own past papers.

## Features

- **Practice**: one real exam problem at a time, easiest first within a topic; moves to neighboring topics when new problems run out; due reviews come first (FSRS).
- **Tracking**: records each subquestion as solved / partly solved / not solved, with hints used and time spent; mastery is computed with Elo, including forgetting decay.
- **Finding problems**: per-school topic frequency (which years each topic appeared and how often), similar problems, and layered search by description (code retrieves candidates, Claude picks).
- **Explanations**: uses solutions in the bank first; otherwise Claude writes one, checks it with sympy / numpy, and saves it as an AI solution for reuse (`data/solutions/<problem ID>.ai.md`). Human solutions go in `<problem ID>.human.md` and take priority over AI ones.
- **Problem panel**: renders formulas and figures in your local browser; can show several problems and their solutions at once.
- **Goals and reports**: exam countdown, study plan, weekly reports, learner profile.

## Layout

| Path | Contents |
|---|---|
| `pipeline/` | Data pipeline: page transcription → problem assembly → linking solutions and errata → tagging → loading (uses the Message Batches API by default) |
| `agent/` | MCP server (23 tools), learning logic, tests |
| `.claude/skills/kakomon-tutor/` | Practice workflow (skill) |
| `web/` | Local web UI: chat, problem area (problem / original scan / solution), "my learning" sidebar, plus an overview page |
| `data/taxonomy/` | Topic taxonomy and aliases (287 topics) |
| `data/viewer/` | Problem panel page |

## Usage

1. Install dependencies (Python 3.12):

   ```bash
   python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
   ```

2. Put your past papers in `kakomon/<folder>/` (PDFs, or one folder of photos per paper). The university, graduate school and program are inferred from the top-level folder name; the mapping is `FOLDER_INFO` in `pipeline/build_manifest.py`. Edit it to match your folders.
3. Create `.env` in the project root with one line: `claude-key=<your Anthropic API key>`.
4. Build the question bank (this costs money: run `estimate` first, and cap spending with `--budget` on `submit`):

   ```bash
   .venv/bin/python -I pipeline/build_manifest.py
   .venv/bin/python -I pipeline/normalize_manifest.py
   .venv/bin/python -I pipeline/run_full.py render --docs all
   .venv/bin/python -I pipeline/run_full.py estimate --docs all
   ```

   Then run the stages `transcribe` → `structure` → `tag`, each with `submit --stage ... --docs all`, `wait` and `collect`; finish with `load` to build the bank and `qa` for a review report. See the docstring at the top of `pipeline/run_full.py`. Reference cost: about 600 papers (about 7,000 pages) cost about $80 in total (Sonnet, Batch API).

   Past exams that are already text (one Markdown file per problem, e.g. [The Kai Project](https://github.com/Myyura/the_kai_project)) can skip transcription and structuring: put them in `kakomon/<folder>/` with an `index.csv` (columns `path, university, graduate_school, program, admission_year, exam_held, paper_title`), run `run_full.py markdown`, then only the `tag` stage and `load`. About $0.008 per problem.
5. Start studying, either way:
   - **Web UI**: chat on the left, the problem (rendered formulas, original scan side by side) and solution in the middle, mastery, learning tendencies and due reviews on the right, plus an overview page.

     ```bash
     .venv/bin/python -I web/server.py
     ```

     Open <http://localhost:18770/>. Each message is handled by your local Claude Code in the background (it must be installed and signed in). Sonnet is the default; you can switch to Opus above the chat box.
     The first start creates the study folder `~/kakomon-study`. To try it without touching your real learning records, add `--sandbox ~/kakomon-try`.
   - **Claude Code in a terminal**:

     ```bash
     .venv/bin/python -I agent/setup_study.py ~/kakomon-study
     ```

     Then `cd ~/kakomon-study && claude` and say something like "I want to practice eigenvalues in linear algebra". Problem panel: <http://localhost:18766/viewer/>.

## Tests

- `agent/test_learner.py`: multi-day simulation on a synthetic bank; needs no real data.
- `agent/smoke_test.py`: calls every tool over the MCP protocol; needs a built bank.
- `agent/dialogue_check.py`: runs a fixed three-turn conversation through headless Claude Code and checks the answers; costs money (about $1 with Sonnet).

## License

[MIT](LICENSE)
