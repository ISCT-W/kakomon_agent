"""
题目面板：把要显示的题目（一道或几道，可附解答）写入 data/viewer/current.json，并在本机启动只读静态服务供面板页读取。

面板页 data/viewer/index.html 每 2 秒读取一次 current.json，用 KaTeX 渲染公式。
注意：MCP 走 stdio，静态服务不能向标准输出打印任何内容。
"""
import json
import os
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
VIEWER_DIR = os.path.join(DATA_DIR, "viewer")
DEFAULT_PORT = 18766


class QuietHandler(SimpleHTTPRequestHandler):
    """
    不输出访问日志、禁止缓存的静态文件处理器。
    """

    def log_message(self, format, *args):
        """
        关闭访问日志（避免干扰 MCP 的 stdio 通道）。
        """
        return

    def end_headers(self):
        """
        追加禁止缓存的响应头，保证面板读到最新题目。
        """
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def start_server(port=None):
    """
    在后台线程启动静态服务（目录为 data/，只监听 127.0.0.1）。

    参数:
        port: 端口，默认环境变量 KAKOMON_VIEWER_PORT 或 18766
    返回:
        str，面板地址；端口被占用时返回已有地址（假定是另一个会话启动的同一服务）
    """
    port = int(port or os.environ.get("KAKOMON_VIEWER_PORT", DEFAULT_PORT))
    url = f"http://localhost:{port}/viewer/"
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(QuietHandler, directory=DATA_DIR))
    except OSError:
        return url
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return url


def rel(path):
    """
    把绝对路径转成相对 data/ 的 URL 路径。

    参数:
        path: 绝对路径
    返回:
        str
    """
    return os.path.relpath(path, DATA_DIR).replace(os.sep, "/") if path else ""


def item(detail, focus_keys=(), solution=None):
    """
    把一道题整理成面板上的一项。

    参数:
        detail: bank.problem_detail 的返回值
        focus_keys: 本次重点小问的规范化键（出题时用）
        solution: solutions.best / solutions.read 的返回值（显示解答时用）
    返回:
        dict
    """
    labels = {s["key"]: s["label"] for s in detail["subquestions"]}
    return {
        "problem_id": detail["problem_id"], "source": detail["source"], "title_zh": detail["title_zh"],
        "difficulty": detail["difficulty"], "est_minutes": detail["est_minutes"],
        "focus": [labels.get(k, k) for k in focus_keys],
        "subquestions": [{k: s[k] for k in ("label", "summary_zh", "difficulty", "concepts")} for s in detail["subquestions"]],
        "content_md": detail["content_md"], "errata_md": detail.get("errata_md", ""),
        "figures": [dict(f, url=rel(f["path"])) for f in detail["figures"] if f.get("path")],
        "page_images": [rel(p) for p in detail["page_images"]],
        "solution": {k: solution.get(k, "") for k in ("label", "text", "covers", "verified", "source")} if solution else None,
    }


def publish(items, note=""):
    """
    把要显示的题目写入面板数据文件（整体替换上一次的内容）。

    参数:
        items: item() 的返回值列表
        note: 面板顶部提示语（如“回炉题”“例题展示（未出题）”）
    """
    out_dir = os.environ.get("KAKOMON_VIEWER_DIR", VIEWER_DIR)
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, "current.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated_at": time.time(), "note": note, "items": items}, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(out_dir, "current.json"))
