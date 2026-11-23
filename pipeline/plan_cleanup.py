"""
列出确定要移除的文件：逐字节重复目录、广告 doc、系统垃圾文件、有日文孪生版的英文版。

只输出清单（TSV：相对路径、原因），不做任何删除；删除由调用方用 trash 执行。

用法:
    python -I pipeline/plan_cleanup.py <kakomon_dir> <out.tsv>
"""
import os
import sys

EN_TWIN_RULES = [("-en.pdf", ".pdf"), ("-e.pdf", "-j.pdf")]


def plan(root):
    """
    生成移除清单。

    参数:
        root: 过去问根目录
    返回:
        list[(相对路径, 原因)]
    """
    items = []
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if os.path.isdir(full) and name.endswith("_1028201517"):
            twin = os.path.join(root, name[: -len("_1028201517")])
            if os.path.isdir(twin):
                items.append((name, "与同名目录逐字节相同的副本"))
    skip = {p for p, _ in items}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir.split(os.sep)[0] in skip:
            continue
        names = set(filenames)
        for f in sorted(filenames):
            rel = os.path.normpath(os.path.join(rel_dir, f))
            if f == ".DS_Store":
                items.append((rel, "macOS 系统文件"))
            elif f == "使用前必看.doc":
                items.append((rel, "二手资料店广告，非题目"))
            else:
                for en_suffix, ja_suffix in EN_TWIN_RULES:
                    if f.endswith(en_suffix) and f[: -len(en_suffix)] + ja_suffix in names:
                        items.append((rel, "英文版，保留日文孪生版 " + f[: -len(en_suffix)] + ja_suffix))
                        break
    return items


def main():
    """
    写出清单并打印各类数量。
    """
    root, out = sys.argv[1], sys.argv[2]
    items = plan(root)
    with open(out, "w", encoding="utf-8") as w:
        w.write("path\treason\n")
        for p, r in items:
            w.write(f"{p}\t{r}\n")
    counts = {}
    for _, r in items:
        key = r.split("，")[0]
        counts[key] = counts.get(key, 0) + 1
    print(len(items), counts)


if __name__ == "__main__":
    main()
