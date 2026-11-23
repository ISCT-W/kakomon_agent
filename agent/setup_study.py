"""
建一个学习用目录：只放刷题需要的配置（MCP 服务、kakomon-tutor 技能、简短说明、默认模型 Sonnet），
不带开发用的 CLAUDE.md 和管线说明，会话上下文更小、更便宜。题库、学习记录、解答存档仍用本仓库 data/ 下的文件。

建好后：cd 到该目录运行 claude 即可。技能是指向本仓库的符号链接，仓库更新后不用重建。

用法:
    python -I agent/setup_study.py ~/kakomon-study
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE = """# 过去问学习

这里是学习用目录：用 kakomon-tutor 技能刷日本大学院入试（情報系）过去问，题库与学习记录在 MCP 服务 kakomon 里。

- 全程用中文，回答简洁：先给结论，再给必要的步骤。
- 计算核对用 `.venv/bin/python -I -c ...`（有 numpy、sympy）。
- 渲染好的题目和解答在题目面板：http://localhost:18766/viewer/
"""


def link(src, dst):
    """
    建符号链接（已存在时跳过）。

    参数:
        src: 指向的路径
        dst: 链接路径
    """
    if not os.path.lexists(dst):
        os.symlink(src, dst)


def main():
    """
    在指定目录写入 .mcp.json、.claude/settings.json、技能链接、解释器链接和 CLAUDE.md。
    """
    if len(sys.argv) != 2:
        sys.exit("用法: python -I agent/setup_study.py <目录>")
    target = os.path.abspath(os.path.expanduser(sys.argv[1]))
    if os.path.commonpath([target, ROOT]) == ROOT:
        sys.exit("学习目录不能放在仓库里面（否则还会读到仓库的 CLAUDE.md）")
    os.makedirs(os.path.join(target, ".claude", "skills"), exist_ok=True)
    python = os.path.join(target, ".venv", "bin", "python")
    link(os.path.realpath(os.path.join(ROOT, ".venv")), os.path.join(target, ".venv"))
    link(os.path.join(ROOT, ".claude", "skills", "kakomon-tutor"), os.path.join(target, ".claude", "skills", "kakomon-tutor"))
    files = {
        ".mcp.json": {"mcpServers": {"kakomon": {"type": "stdio", "command": python,
                                                 "args": ["-I", os.path.join(ROOT, "agent", "server.py")]}}},
        os.path.join(".claude", "settings.json"): {
            "model": "sonnet", "enabledMcpjsonServers": ["kakomon"],
            "permissions": {"allow": ["mcp__kakomon", "Bash(.venv/bin/python -I -c:*)"]}},
    }
    for name, data in files.items():
        with open(os.path.join(target, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    with open(os.path.join(target, "CLAUDE.md"), "w", encoding="utf-8") as f:
        f.write(GUIDE)
    print(f"已建好：{target}\n开始学习：cd {target} && claude（默认 Sonnet；讲难题时可用 /model 换 Opus）")


if __name__ == "__main__":
    main()
