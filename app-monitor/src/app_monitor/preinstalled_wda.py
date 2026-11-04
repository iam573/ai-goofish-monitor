"""Connect externally signed WDA without invoking local Xcode or changing teams."""
from __future__ import annotations

import json
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

from .appium_server import WDA_PORT
from .config import ROOT
from .stop_control import checkpoint, run_process


def _close_forwarder(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    for stream in (process.stdin, process.stdout):
        if stream:
            stream.close()


def connect_preinstalled_wda(settings, udid, capabilities, factory):
    bundle = settings.preinstalled_wda_bundle(udid)
    node = shutil.which('node')
    if not node:
        raise RuntimeError("已安装 WDA 连接需要 Node.js")
    checkpoint()
    # Launch only this device's externally signed runner. Never install, re-sign,
    # uninstall, or substitute a locally built app on this path.
    env = {"USE_PORT": str(WDA_PORT), "WDA_PRODUCT_BUNDLE_IDENTIFIER": bundle,
           "MJPEG_SERVER_PORT": str(WDA_PORT + 1000)}
    result = run_process(
        ['xcrun', 'devicectl', 'device', 'process', 'launch', '--device', udid,
         '--no-activate', '--terminate-existing', '--environment-variables', json.dumps(env), bundle],
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("已安装 WebDriverAgent 启动失败，请检查手机解锁、开发者信任和签名有效期")
    checkpoint()
    # On this iOS 27/XCTest stack --no-activate still leaves the runner in
    # front. Launching the collection app backgrounds it before XCTest's
    # 30-second initialization deadline, allowing its HTTP server to start.
    foreground = run_process(
        ['xcrun', 'devicectl', 'device', 'process', 'launch', '--device', udid, settings.bundle_id],
        timeout=20,
    )
    if foreground.returncode:
        raise RuntimeError("无法打开闲鱼以完成 WebDriverAgent 后台初始化，请检查手机解锁及闲鱼安装")
    checkpoint()
    process = subprocess.Popen(
        [node, str(Path(__file__).with_suffix('.cjs')), str(ROOT), udid, str(WDA_PORT)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=5):
                raise RuntimeError("WebDriverAgent USB 转发启动超时")
            port = int(json.loads(process.stdout.readline())['port'])
        url = f'http://127.0.0.1:{port}'
        deadline = time.monotonic() + 20
        while True:
            checkpoint()
            try:
                with urlopen(url + '/status', timeout=1) as response:
                    status = json.load(response)['value']
                if status.get('build', {}).get('productBundleIdentifier') != bundle:
                    raise RuntimeError("手机运行的 WebDriverAgent 与配置包名不一致")
                if status.get('ready') is True:
                    break
            except (OSError, ValueError, KeyError):
                pass
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("已安装 WebDriverAgent 未就绪，请检查手机解锁、信任和签名有效期")
            time.sleep(0.2)
        checkpoint()
        for key in ('appium:usePrebuiltWDA', 'appium:derivedDataPath',
                    'appium:wdaLocalPort', 'appium:wdaRemotePort'):
            capabilities.pop(key, None)
        capabilities.update({'appium:webDriverAgentUrl': url,
                             'appium:updatedWDABundleId': bundle.removesuffix('.xctrunner'),
                             'appium:waitForIdleTimeout': 0})
        capabilities['appium:settings'] = {
            **capabilities.get('appium:settings', {}), 'waitForIdleTimeout': 0,
            'defaultActiveApplication': settings.bundle_id,
        }
        driver = factory(capabilities)
        original_quit = driver.quit

        def quit_and_close():
            try:
                return original_quit()
            finally:
                _close_forwarder(process)

        driver.quit = quit_and_close
        return driver
    except BaseException:
        _close_forwarder(process)
        raise
