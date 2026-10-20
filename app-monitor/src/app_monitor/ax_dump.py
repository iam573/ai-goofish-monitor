"""调试点击路径：打开搜索、按路径点击，然后列出当前闲鱼窗口里的短文字及位置。

用法（在 app-monitor 目录下）::

    uv run python -m app_monitor.ax_dump "iPhone 15 Pro"
    uv run python -m app_monitor.ax_dump "iPhone 15 Pro" --path "区域 > 福建 > 厦门"
    uv run python -m app_monitor.ax_dump --path "筛选"          # 不重新搜索，在当前页面继续点

输出里能看到 App 实际的按钮文字（例如「查看」「256GB」「确定」），据此调整后台的点击路径。
完整节点同时保存到 data/debug/ 下，方便发给别人排查。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from urllib.parse import quote

from .collector import AXSnapshot
from .config import settings
from .rules import split_path


async def main(keyword: str | None, path: str, max_length: int) -> None:
    accessibility = AXSnapshot(settings.bundle_id)
    if keyword:
        link = (
            "fleamarket://idle_search_result?keyword="
            f"{quote(keyword, safe='')}&searchReqFromPage=home&searchTabType=SEARCH_TAB_MAIN"
        )
        process = await asyncio.create_subprocess_exec("open", link)
        await process.wait()
        await asyncio.sleep(3)
    if path:
        try:
            result = await accessibility.click_path(split_path(path))
            print(f"点击完成：{result}")
        except Exception as exc:
            print(f"点击失败：{exc}")
        await asyncio.sleep(2)

    nodes = await accessibility.nodes()
    rows = []
    for node in nodes:
        text = next(
            (str(node[key]).strip() for key in ("title", "value", "description") if str(node.get(key) or "").strip()),
            "",
        )
        if not text or len(text) > max_length:
            continue
        position = node.get("position") or {}
        rows.append((round(float(position.get("y", 0))), round(float(position.get("x", 0))), node.get("role", ""), text))
    print(f"\n当前窗口共 {len(rows)} 个短文字（≤{max_length} 字），按位置从上到下：\n")
    print(f"{'y':>6} {'x':>6}  {'role':<18} 文字")
    for y, x, role, text in sorted(rows):
        print(f"{y:>6} {x:>6}  {role:<18} {text}")

    directory = settings.data_dir / "debug"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"ax-{datetime.now():%Y%m%d-%H%M%S}.json"
    target.write_text(json.dumps(nodes, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n完整节点已保存：{target}")


def cli() -> None:
    parser = argparse.ArgumentParser(description="列出闲鱼窗口里的按钮文字，用于调整点击路径")
    parser.add_argument("keyword", nargs="?", help="先用该关键词打开搜索；省略则在当前页面操作")
    parser.add_argument("--path", default="", help="依次点击，例如「区域 > 福建 > 厦门」")
    parser.add_argument("--max-length", type=int, default=12, help="只列出不超过该长度的文字")
    args = parser.parse_args()
    asyncio.run(main(args.keyword, args.path, args.max_length))


if __name__ == "__main__":
    cli()
