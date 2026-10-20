#!/usr/bin/env python3
"""Open a Xianyu search result through the official Mac app URL scheme."""

from __future__ import annotations

import argparse
import subprocess
from urllib.parse import urlencode


def build_search_url(keyword: str) -> str:
    query = urlencode(
        {
            "keyword": keyword,
            "searchReqFromPage": "home",
            "searchTabType": "SEARCH_TAB_MAIN",
        }
    )
    return f"fleamarket://idle_search_result?{query}"


def main() -> None:
    parser = argparse.ArgumentParser(description="让 Mac 版闲鱼打开商品结果页。")
    parser.add_argument("keyword", help="搜索关键词")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    keyword = args.keyword.strip()
    if not keyword:
        parser.error("关键词不能为空")
    url = build_search_url(keyword)
    if args.dry_run:
        print(url)
        return
    subprocess.run(["open", url], check=True)
    print(f"已触发闲鱼搜索：{keyword}")


if __name__ == "__main__":
    main()
