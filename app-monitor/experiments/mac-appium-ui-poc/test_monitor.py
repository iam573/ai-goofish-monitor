import unittest
from unittest.mock import patch

from monitor import discover_xianyu_apps, extract_items_from_source


SAMPLE_SOURCE = """
<AppiumAUT type="XCUIElementTypeApplication">
  <XCUIElementTypeCell type="XCUIElementTypeCell">
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="MacBook Air M1 8+256 自用" />
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="¥2999" />
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="上海" />
  </XCUIElementTypeCell>
  <XCUIElementTypeCell type="XCUIElementTypeCell">
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="iPhone 15 Pro 国行" />
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="￥4999元" />
  </XCUIElementTypeCell>
</AppiumAUT>
"""


class ExtractItemsTest(unittest.TestCase):
    def test_discovers_app_store_installed_app(self):
        payload = {"result": {"apps": [{"bundleIdentifier": "com.taobao.fleamarket", "name": "闲鱼"}]}}
        with patch("monitor._run_json", return_value=payload) as run_json:
            apps = discover_xianyu_apps("device-id")
        self.assertEqual("com.taobao.fleamarket", apps[0].bundle_id)
        self.assertIn("--include-default-apps", run_json.call_args.args[0])

    def test_extracts_and_deduplicates_cards(self):
        items = extract_items_from_source(SAMPLE_SOURCE)
        self.assertEqual(2, len(items))
        by_title = {item.title: item for item in items}
        self.assertEqual("¥2999", by_title["MacBook Air M1 8+256 自用"].price)
        self.assertEqual("¥4999", by_title["iPhone 15 Pro 国行"].price)


if __name__ == "__main__":
    unittest.main()
