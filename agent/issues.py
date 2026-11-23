"""
题目报错的维护工具：列出学生报告的转写/标签错误，修正后标为已处理（题目恢复出题）。

用法:
    python -I agent/issues.py list [--status open|resolved|all]
    python -I agent/issues.py resolve <报错ID> [--note 处理说明]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import learner  # noqa: E402


def main():
    """
    命令行入口。
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["list", "resolve"])
    ap.add_argument("issue_id", nargs="?", type=int)
    ap.add_argument("--status", default="open", choices=["open", "resolved", "all"])
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    con = learner.connect()
    if args.command == "list":
        for i in learner.list_issues(con, args.status):
            print(f"#{i['id']} {i['ts']} {i['problem_id']} [{i['status']}] {i['description']}"
                  + (f"（处理：{i['resolution']}）" if i["resolution"] else ""))
        return
    if not args.issue_id:
        raise SystemExit("resolve 需要报错 ID")
    learner.resolve_issue(con, args.issue_id, args.note)
    print(f"#{args.issue_id} 已标为已处理")


if __name__ == "__main__":
    main()
