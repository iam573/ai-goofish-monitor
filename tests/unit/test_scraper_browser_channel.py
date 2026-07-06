import importlib


def _load_scraper(monkeypatch, *, login_is_edge: bool, running_in_docker: bool):
    monkeypatch.setenv("LOGIN_IS_EDGE", "true" if login_is_edge else "false")
    monkeypatch.setenv("RUNNING_IN_DOCKER", "true" if running_in_docker else "false")

    import src.config as config_module
    import src.scraper as scraper_module

    importlib.reload(config_module)
    reloaded_scraper = importlib.reload(scraper_module)
    reloaded_scraper.EDGE_DOCKER_WARNING_PRINTED = False
    return reloaded_scraper


def test_resolve_browser_channel_uses_chromium_in_docker_even_when_edge_requested(monkeypatch, capsys):
    scraper = _load_scraper(monkeypatch, login_is_edge=True, running_in_docker=True)

    assert scraper._resolve_browser_channel() == "chromium"
    assert "Docker 镜像未内置 Edge" in capsys.readouterr().out


def test_resolve_browser_channel_uses_msedge_locally_when_requested(monkeypatch):
    scraper = _load_scraper(monkeypatch, login_is_edge=True, running_in_docker=False)

    assert scraper._resolve_browser_channel() == "msedge"


def test_build_extra_headers_filters_navigation_and_browser_managed_headers(monkeypatch):
    scraper = _load_scraper(monkeypatch, login_is_edge=False, running_in_docker=False)

    assert scraper._build_extra_headers(
        {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en,zh-CN;q=0.9",
            "Cookie": "redacted",
            "Referer": "https://www.goofish.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "sec-ch-ua": '"Google Chrome";v="149"',
            "User-Agent": "Mozilla/5.0",
            "X-Custom-Trace": "keep-me",
        }
    ) == {"X-Custom-Trace": "keep-me"}
