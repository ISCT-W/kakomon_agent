"""
数据管线公共工具：读取密钥、打开（含加密）PDF、渲染页面、调用 Claude 结构化输出并统计费用。
"""
import base64
import glob
import io
import json
import os

import anthropic
import pymupdf
from PIL import Image, ImageOps

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KAKOMON_DIR = os.path.join(PROJECT_DIR, "kakomon")
DATA_DIR = os.path.join(PROJECT_DIR, "data")

MODEL = "claude-sonnet-5-5"
PRICE_PER_MTOK = {"input": 2.00, "output": 10.00, "cache_read": 0.10, "cache_write": 2.50}
BATCH_DISCOUNT = 0.5
FALLBACK_BETA = "server-side-fallback-2026-07-01"
IMAGE_EXTS = (".jpg", ".jpeg", ".png")


def load_api_key(env_path=None):
    """
    从项目根目录的 .env 读取 API 密钥。

    参数:
        env_path: .env 路径，默认项目根目录下的 .env
    返回:
        str，密钥；找不到时抛出 RuntimeError
    """
    env_path = env_path or os.path.join(PROJECT_DIR, ".env")
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() in ("claude-key", "CLAUDE_KEY", "ANTHROPIC_API_KEY"):
                return value.strip().strip('"').strip("'")
    raise RuntimeError("在 .env 中找不到 claude-key")


def make_client():
    """
    创建 Anthropic 客户端（自动重试 5 次）。

    返回:
        anthropic.Anthropic 实例
    """
    return anthropic.Anthropic(api_key=load_api_key(), max_retries=5, timeout=300.0)


def folder_password(pdf_path):
    """
    在 PDF 所在目录查找文件名形如“密码xxx.txt”的文件，返回其中的密码。

    参数:
        pdf_path: PDF 路径
    返回:
        str 或 None
    """
    for txt in glob.glob(os.path.join(os.path.dirname(pdf_path), "密码*.txt")):
        return os.path.basename(txt)[len("密码"):-len(".txt")]
    return None


def open_pdf(path):
    """
    打开 PDF；若加密则用同目录“密码*.txt”文件名中的密码解锁。

    参数:
        path: PDF 路径
    返回:
        pymupdf.Document
    """
    doc = pymupdf.open(path)
    if doc.needs_pass:
        pw = folder_password(path)
        if not pw or not doc.authenticate(pw):
            raise RuntimeError(f"无法解锁加密 PDF: {path}")
    return doc


def page_image(path, page_index=0, long_edge=2000):
    """
    把 PDF 的某一页或一张照片渲染成 RGB 图像，长边不超过 long_edge。

    参数:
        path: PDF 或图片路径
        page_index: PDF 页码（从 0 开始）；图片忽略
        long_edge: 输出长边像素
    返回:
        PIL.Image（RGB）
    """
    if path.lower().endswith(IMAGE_EXTS):
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        img.thumbnail((long_edge, long_edge), Image.LANCZOS)
        return img
    page = open_pdf(path)[page_index]
    zoom = long_edge / max(page.rect.width, page.rect.height)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csRGB)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def content_ratio(img):
    """
    计算非白像素比例（灰度<230），低于约 0.03 的页面基本是空白或“余白”模板页。

    参数:
        img: PIL 图像
    返回:
        float
    """
    small = img.convert("L").resize((160, int(160 * img.height / img.width)))
    data = small.tobytes()
    return sum(1 for b in data if b < 230) / max(1, len(data))


def image_block(img, quality=90):
    """
    把 PIL 图像编码为 API 的 base64 JPEG 图像内容块。

    参数:
        img: PIL 图像
        quality: JPEG 质量
    返回:
        dict，image 内容块
    """
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    data = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def usage_cost(usage, batch=False):
    """
    根据 usage 计算本次调用费用（美元，Sonnet 5.5 价格；Batch 为标准价的一半）。

    参数:
        usage: 响应中的 usage 对象
        batch: 是否经由 Batch API
    返回:
        (dict 用量明细, float 费用)
    """
    u = {
        "input": getattr(usage, "input_tokens", 0) or 0,
        "output": getattr(usage, "output_tokens", 0) or 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }
    cost = sum(u[k] * PRICE_PER_MTOK[k] for k in u) / 1_000_000
    return u, cost * (BATCH_DISCOUNT if batch else 1.0)


def request_params(system, content, schema, effort="low", max_tokens=16000):
    """
    组装一次结构化输出请求的参数（同步调用与 Batch 共用，保证两种方式的请求完全一致）。

    参数:
        system: 系统提示（会被缓存）
        content: 用户消息内容块列表
        schema: JSON Schema（对象需 additionalProperties: false）
        effort: 推理强度 low/medium/high
        max_tokens: 最大输出 token
    返回:
        dict，可直接作为 messages.create 的参数或 Batch 请求的 params
    """
    return {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": content}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    }


def parse_message(msg, batch=False):
    """
    解析一条结构化输出响应（同步响应或 Batch 结果中的 message）。

    参数:
        msg: Message 对象
        batch: 是否经由 Batch API（决定计价）
    返回:
        dict，包含 data（解析后的 JSON 或 None）、stop_reason、usage、cost、model、error
    """
    usage, cost = usage_cost(msg.usage, batch)
    result = {"data": None, "stop_reason": msg.stop_reason, "usage": usage, "cost": cost,
              "model": msg.model, "error": ""}
    if msg.stop_reason in ("refusal", "max_tokens"):
        result["error"] = msg.stop_reason
        return result
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        result["data"] = json.loads(text)
    except json.JSONDecodeError as e:
        result["error"] = f"json: {e}"
    return result


def call_json(client, system, content, schema, effort="low", max_tokens=16000):
    """
    同步调用 Claude 并强制返回符合 JSON Schema 的结果；被安全分类器拒绝时由服务端自动换模型重试。

    参数:
        client: anthropic 客户端
        system: 系统提示（会被缓存）
        content: 用户消息内容块列表
        schema: JSON Schema（对象需 additionalProperties: false）
        effort: 推理强度 low/medium/high
        max_tokens: 最大输出 token
    返回:
        dict，见 parse_message
    """
    return call_params(client, request_params(system, content, schema, effort, max_tokens))


def call_params(client, params):
    """
    用 request_params 组好的参数同步调用；被安全分类器拒绝时由服务端自动换模型重试。

    参数:
        client: anthropic 客户端
        params: request_params 的返回值
    返回:
        dict，见 parse_message
    """
    resp = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **params)
    return parse_message(resp)
