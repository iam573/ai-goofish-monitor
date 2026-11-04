"""Renew a mapped phone's WDA on an SSH builder and install it over local USB."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import plistlib
import shlex
import shutil
import tarfile
import tempfile
import time

from .config import ROOT
from .stop_control import checkpoint, run_process
from .wda_maintenance import APP, WdaMaintenance, action_for_error, replace_products


def run(command, *, timeout=60):
    completed = run_process(command, timeout=timeout)
    if completed.returncode:
        details = (completed.stderr or completed.stdout)[-8000:]
        if command[0] in {'ssh', 'scp'} and any(word in details.lower() for word in (
                'connection refused', 'no route to host', 'connection reset', 'connection closed',
                'could not resolve hostname', 'operation timed out')):
            raise RuntimeError('Remote WDA network connection was lost: ' + details)
        raise RuntimeError('Remote WDA: ' + details)
    return completed.stdout


class RemoteWdaMaintenance(WdaMaintenance):
    def __init__(self, settings, database, udid):
        bundle = settings.preinstalled_wda_bundle(udid)
        if not re.fullmatch(r'[A-Za-z0-9_.@:-]+', settings.ios_wda_remote_host) or settings.ios_wda_remote_host.startswith('-'):
            raise ValueError('Invalid IOS_WDA_REMOTE_HOST')
        if not settings.ios_wda_remote_team_id:
            raise RuntimeError('Remote WDA signing development team is missing')
        self.host = settings.ios_wda_remote_host
        suffix = hashlib.sha256((udid + bundle).encode()).hexdigest()[:24]
        cache = ROOT / '.runtime/remote-wda' / suffix
        super().__init__(replace(settings, ios_wda_bundle_id=bundle.removesuffix('.xctrunner'),
                                 ios_wda_team_id=settings.ios_wda_remote_team_id,
                                 ios_wda_derived_data_path=cache), database)

    def attention(self, udid, error):
        code, _ = action_for_error(error)
        if code == 'keychain':
            return self._attention_action(udid, code,
                '请在远端构建 Mac 解锁登录钥匙串并允许签名，程序将在后续搜索自动重试。')
        return self._attention_action(udid, code,
            '远端 WDA 维护失败，请检查构建 Mac 的 SSH 连接、Xcode 账号、签名及登录钥匙串；修复后点击立即重试。')

    def _valid(self, profile, udid, margin=0):
        expected = self.settings.ios_wda_team_id + '.' + self.settings.ios_wda_bundle_id + '.xctrunner'
        allowed = profile.get('identifier', '')
        matches = allowed == expected or (allowed.endswith('.*') and expected.startswith(allowed[:-1]))
        return (profile.get('expires', 0) > time.time() + margin
                and udid in profile.get('devices', [])
                and profile.get('team') == self.settings.ios_wda_team_id and matches)

    def _read_profile(self, app):
        profile = super()._read_profile(app)
        if not profile:
            return profile
        try:
            with tempfile.TemporaryDirectory(prefix='goofish-wda-cert-') as directory:
                prefix = str(Path(directory) / 'cert')
                run(['codesign', '-d', '--extract-certificates=' + prefix, str(app)], timeout=20)
                end = run(['openssl', 'x509', '-inform', 'DER', '-in', prefix + '0',
                           '-noout', '-enddate'], timeout=20).strip().split('=', 1)[1]
                expires = datetime.strptime(end, '%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc).timestamp()
                return {**profile, 'expires': min(profile['expires'], expires)}
        except (RuntimeError, ValueError, OSError):
            return {}

    def ensure(self, udid, *, force=False):
        app = self.settings.ios_wda_derived_data_path / APP
        # A broken on-phone installation can be repaired from a valid cache,
        # even while the build Mac is offline. Expired signatures still renew.
        if force and self._valid(self._read_profile(app), udid):
            result = True
        else:
            result = super()._ensure(udid, force=force)
        if not self._valid(self._read_profile(app), udid):
            raise RuntimeError('Remote WDA provisioning profile is not valid')
        info = plistlib.loads((app / 'Info.plist').read_bytes())
        fingerprint = hashlib.sha256((app / 'embedded.mobileprovision').read_bytes()
                                     + (app / '_CodeSignature/CodeResources').read_bytes()
                                     + (app / info['CFBundleExecutable']).read_bytes()).hexdigest()
        key = 'wda_remote_installed_' + hashlib.sha256(udid.encode()).hexdigest()
        if force or self.database.state_get(key) != fingerprint:
            checkpoint()
            run(['xcrun', 'devicectl', 'device', 'install', 'app', '--device', udid, str(app)], timeout=120)
            self.database.state_set(key, fingerprint)
        return result

    def _build(self, udid, team):
        derived = self.settings.ios_wda_derived_data_path
        derived.parent.mkdir(parents=True, exist_ok=True)
        source = ROOT / '.appium/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent'
        if not (source / 'WebDriverAgent.xcodeproj').is_dir():
            raise RuntimeError('Remote WDA source project is missing')
        ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', self.host]
        scp = ['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
        base = '.local/share/goofish-wda-builder'
        with tempfile.TemporaryDirectory(prefix='.build-', dir=derived.parent) as temporary:
            staging = Path(temporary)
            archive = staging / 'source.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                for path in sorted(source.rglob('*')):
                    relative = path.relative_to(source)
                    if any(part in {'node_modules', '.git'} for part in relative.parts) or path.is_symlink():
                        continue
                    if path.is_file():
                        tar.add(path, arcname=str(relative), recursive=False)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            run(ssh + ['mkdir -p ' + base])
            run(scp + [str(archive), self.host + ':' + base + '/source-' + digest + '.tar.gz'])
            builder = Path(__file__).with_name('remote_wda_builder.py')
            builder_hash = hashlib.sha256(builder.read_bytes()).hexdigest()
            remote_script = base + '/builder-' + builder_hash + '.py'
            run(scp + [str(builder), self.host + ':' + remote_script])
            command = shlex.join(['python3', remote_script, digest, self.settings.ios_wda_bundle_id, team, udid])
            manifest = json.loads(run(ssh + [command], timeout=700))
            if (manifest.get('bundle_id') != self.settings.ios_wda_bundle_id + '.xctrunner'
                    or manifest.get('team') != team):
                raise RuntimeError('Remote WDA build manifest mismatch')
            artifact = manifest.get('artifact', '')
            expected = base + '/artifacts/' + hashlib.sha256((udid + self.settings.ios_wda_bundle_id).encode()).hexdigest()[:24] + '/WDA.zip'
            if artifact != expected:
                raise RuntimeError('Remote WDA artifact path mismatch')
            package = staging / 'WDA.zip'
            run(scp + [self.host + ':' + artifact, str(package)])
            if hashlib.sha256(package.read_bytes()).hexdigest() != manifest.get('sha256'):
                raise RuntimeError('Remote WDA download checksum mismatch')
            candidate = staging / 'candidate'
            run(['ditto', '-x', '-k', str(package), str(candidate)])
            app = candidate / 'WebDriverAgentRunner-Runner.app'
            if not self._valid(self._read_profile(app), udid):
                raise RuntimeError('Remote WDA downloaded provisioning profile mismatch')
            info = plistlib.loads((app / 'Info.plist').read_bytes())
            if info.get('CFBundleIdentifier') != manifest['bundle_id']:
                raise RuntimeError('Remote WDA downloaded bundle identifier mismatch')
            run(['codesign', '--verify', '--deep', '--strict', str(app)], timeout=30)
            fresh = staging / 'products/Debug-iphoneos'
            fresh.mkdir(parents=True)
            shutil.move(str(app), fresh / app.name)
            replace_products(derived, staging / 'products', staging / 'previous-products')
