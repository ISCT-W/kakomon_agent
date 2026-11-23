"""
用页面感知哈希（dHash）在整个过去问目录里找内容重复的文件。

对每个 PDF 页面和每张照片计算 256 位 dHash，跳过近乎空白的页面，
统计两两文件之间互相匹配的页面数，输出疑似重复的文件对。

用法:
    python -I pipeline/find_duplicates.py <kakomon_dir> <out.tsv> [max_hamming]
"""
import os
import sys

import numpy as np
import pymupdf
from PIL import Image, ImageOps

THUMB_WIDTH = 160
CONTENT_INK = 0.03
INK_GRAY = 230
SKIP_DIR_SUFFIX = "_1028201517"


def ink_ratio(img):
    """
    计算灰度图中非白像素的占比；低于 CONTENT_INK 的页（空白页、“余白”模板页）不参与比对。

    参数:
        img: PIL 灰度图
    返回:
        float，非白像素比例
    """
    arr = np.asarray(img)
    return float((arr < INK_GRAY).mean())


def dhash256(img):
    """
    计算 256 位差值哈希。

    参数:
        img: PIL 灰度图
    返回:
        numpy uint64 数组，长度 4
    """
    small = np.asarray(img.resize((17, 16), Image.BILINEAR), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    packed = np.packbits(bits).view(">u8").astype(np.uint64)
    return packed


def iter_pages(path):
    """
    逐页产出缩略灰度图；照片文件当作单页处理。

    参数:
        path: 文件路径（pdf/jpg/png）
    返回:
        生成器，产出 (页码, PIL 灰度图)
    """
    low = path.lower()
    if low.endswith((".jpg", ".jpeg", ".png")):
        img = ImageOps.exif_transpose(Image.open(path)).convert("L")
        img.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 2))
        yield 0, img
        return
    doc = pymupdf.open(path)
    if doc.needs_pass:
        return
    for i, page in enumerate(doc):
        zoom = THUMB_WIDTH / max(1.0, page.rect.width)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY)
        yield i, Image.frombytes("L", (pix.width, pix.height), pix.samples)


def collect(root):
    """
    遍历目录，收集所有非空白页的哈希。

    参数:
        root: 过去问根目录
    返回:
        (文件相对路径列表, 每页所属文件下标数组, 哈希矩阵 N x 4, 每个文件非空白页数)
    """
    files, owners, hashes = [], [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.endswith(SKIP_DIR_SUFFIX))
        for name in sorted(filenames):
            if not name.lower().endswith((".pdf", ".jpg", ".jpeg", ".png")):
                continue
            full = os.path.join(dirpath, name)
            fid = len(files)
            files.append(os.path.relpath(full, root))
            for _, img in iter_pages(full):
                if ink_ratio(img) < CONTENT_INK:
                    continue
                owners.append(fid)
                hashes.append(dhash256(img))
    owners = np.array(owners, dtype=np.int32)
    counts = np.bincount(owners, minlength=len(files))
    return files, owners, np.vstack(hashes), counts


def match_pairs(owners, hashes, max_ham):
    """
    找出跨文件的相似页面对。

    参数:
        owners: 每页所属文件下标
        hashes: 哈希矩阵 N x 4
        max_ham: 判定为同一页的最大汉明距离
    返回:
        dict，键为 (文件a, 文件b)，值为 (a 中被匹配的页集合, b 中被匹配的页集合, 距离列表)
    """
    pairs = {}
    n = len(owners)
    for i in range(n - 1):
        diff = np.bitwise_xor(hashes[i + 1:], hashes[i])
        dist = np.bitwise_count(diff).sum(axis=1)
        for k in np.nonzero(dist <= max_ham)[0]:
            j = i + 1 + int(k)
            fa, fb = int(owners[i]), int(owners[j])
            if fa == fb:
                continue
            key = (fa, fb) if fa < fb else (fb, fa)
            pa, pb = (i, j) if fa < fb else (j, i)
            entry = pairs.setdefault(key, (set(), set(), []))
            entry[0].add(pa)
            entry[1].add(pb)
            entry[2].append(int(dist[k]))
    return pairs


def main():
    """
    读取参数，计算并输出疑似重复文件对（按覆盖率排序）。
    """
    root, out = sys.argv[1], sys.argv[2]
    max_ham = int(sys.argv[3]) if len(sys.argv) > 3 else 24
    files, owners, hashes, counts = collect(root)
    pairs = match_pairs(owners, hashes, max_ham)
    rows = []
    for (fa, fb), (sa, sb, dists) in pairs.items():
        if min(len(sa), len(sb)) < 2:
            continue
        cov_a = len(sa) / counts[fa]
        cov_b = len(sb) / counts[fb]
        if max(cov_a, cov_b) < 0.5:
            continue
        rows.append((max(cov_a, cov_b), files[fa], int(counts[fa]), len(sa), cov_a,
                     files[fb], int(counts[fb]), len(sb), cov_b, float(np.mean(dists))))
    rows.sort(key=lambda r: -r[0])
    with open(out, "w", encoding="utf-8") as w:
        w.write("file_a\tpages_a\tmatched_a\tcover_a\tfile_b\tpages_b\tmatched_b\tcover_b\tmean_dist\n")
        for r in rows:
            w.write(f"{r[1]}\t{r[2]}\t{r[3]}\t{r[4]:.2f}\t{r[5]}\t{r[6]}\t{r[7]}\t{r[8]:.2f}\t{r[9]:.1f}\n")
    print(f"files={len(files)} nonblank_pages={len(owners)} suspicious_pairs={len(rows)}")


if __name__ == "__main__":
    main()
