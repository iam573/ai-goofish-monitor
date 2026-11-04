"""Run over SSH with Python's standard library; signing keys stay on the builder."""
from __future__ import annotations

import fcntl
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid


def run(command, *, timeout=600):
    env = {key: value for key, value in os.environ.items() if key not in {
        'SDKROOT', 'CPATH', 'C_INCLUDE_PATH', 'CPLUS_INCLUDE_PATH', 'OBJC_INCLUDE_PATH',
        'LIBRARY_PATH', 'CFLAGS', 'CPPFLAGS'}}
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    if result.returncode:
        errors = [line for line in (result.stdout + '\n' + result.stderr).splitlines()
                  if re.search(r'(?i)error:|errSecInternalComponent|no accounts|session has expired|user interaction is not allowed', line)]
        details = '\n'.join(errors)[-6000:]
        if not details and command[0] in {'codesign', 'openssl', 'launchctl'}:
            details = (result.stderr + result.stdout)[-2000:]
        raise RuntimeError(details or 'Remote WDA command failed: ' + command[0])
    return result.stdout


def build(source_hash, bundle, team, udid):
    if not re.fullmatch(r'[a-f0-9]{64}', source_hash):
        raise ValueError('Invalid source hash')
    if not all(re.fullmatch(r'[A-Za-z0-9.-]+', value) for value in (bundle, team, udid)):
        raise ValueError('Invalid WDA build parameters')
    base = Path.home() / '.local/share/goofish-wda-builder'
    archive = base / ('source-' + source_hash + '.tar.gz')
    if hashlib.sha256(archive.read_bytes()).hexdigest() != source_hash:
        raise ValueError('Source archive checksum mismatch')
    identity = hashlib.sha256((udid + bundle).encode()).hexdigest()[:24]
    output = base / 'artifacts' / identity
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix='job-', dir=output) as temporary:
            staging = Path(temporary)
            source = staging / 'source'
            source.mkdir()
            with tarfile.open(archive) as tar:
                for member in tar.getmembers():
                    target = (source / member.name).resolve()
                    if source.resolve() not in target.parents or member.issym() or member.islnk():
                        raise ValueError('Unsafe source archive member')
                tar.extractall(source)
            project = source / 'WebDriverAgent.xcodeproj'
            pbx = project / 'project.pbxproj'
            text = pbx.read_text()
            text = re.sub(r'PRODUCT_BUNDLE_IDENTIFIER = [^;]*WebDriverAgentRunner[^;]*;',
                          'PRODUCT_BUNDLE_IDENTIFIER = ' + bundle.removesuffix('.xctrunner') + ';', text)
            pbx.write_text(text)
            derived = staging / 'derived'
            run(['xcodebuild', '-quiet', '-project', str(project), '-scheme', 'WebDriverAgentRunner',
                 '-configuration', 'Debug', '-sdk', 'iphoneos', '-destination', 'generic/platform=iOS',
                 '-derivedDataPath', str(derived), '-allowProvisioningUpdates', 'build-for-testing',
                 'DEVELOPMENT_TEAM=' + team, 'CODE_SIGN_STYLE=Automatic',
                 'CODE_SIGN_IDENTITY=Apple Development'])
            app = derived / 'Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app'
            profile = plistlib.loads(subprocess.run(
                ['security', 'cms', '-D', '-i', str(app / 'embedded.mobileprovision')],
                capture_output=True, check=True).stdout)
            runner = bundle.removesuffix('.xctrunner') + '.xctrunner'
            allowed = profile.get('Entitlements', {}).get('application-identifier', '')
            expected = team + '.' + runner
            matches = allowed == expected or (allowed.endswith('.*') and expected.startswith(allowed[:-1]))
            if (profile.get('TeamIdentifier') != [team]
                    or not matches
                    or udid not in profile.get('ProvisionedDevices', [])):
                raise RuntimeError('Provisioning profile does not match team, runner or target device')
            info = plistlib.loads((app / 'Info.plist').read_bytes())
            if info.get('CFBundleIdentifier') != runner:
                raise RuntimeError('Built WDA bundle identifier mismatch')
            # A standalone runner must load XCTest from the phone, rather than
            # embedded copies generated for Xcode's test-without-building path.
            run(['codesign', '-d', '--extract-certificates=' + str(staging / 'leaf'), str(app)], timeout=30)
            fingerprint = hashlib.sha1((staging / 'leaf0').read_bytes()).hexdigest().upper()
            expiry = run(['openssl', 'x509', '-inform', 'DER', '-in', str(staging / 'leaf0'),
                          '-noout', '-enddate'], timeout=20).strip().split('=', 1)[1]
            certificate_expires = datetime.strptime(expiry, '%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc)
            expires = min(profile['ExpirationDate'].replace(tzinfo=timezone.utc), certificate_expires)
            if expires.timestamp() <= time.time():
                raise RuntimeError('WDA signing certificate or profile has expired')
            frameworks = app / 'Frameworks'
            for child in list(frameworks.iterdir()) if frameworks.exists() else []:
                if child.name.startswith('XC') or child.name in {'Testing.framework', 'libXCTestSwiftSupport.dylib'}:
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            run(['codesign', '--force', '--sign', fingerprint,
                 '--preserve-metadata=identifier,entitlements,flags', '--generate-entitlement-der', str(app)], timeout=60)
            run(['codesign', '--verify', '--deep', '--strict', str(app)], timeout=30)
            candidate = staging / 'WDA.zip'
            run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(candidate)], timeout=60)
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            os.replace(candidate, output / 'WDA.zip')
            return {'artifact': str((output / 'WDA.zip').relative_to(Path.home())),
                    'sha256': digest, 'bundle_id': runner, 'team': team,
                    'expires': expires.isoformat(), 'certificate_expires': certificate_expires.isoformat()}


def gui_build(arguments):
    # Use the logged-in desktop session's keychain access. SSH audit sessions
    # have separate keychain unlock state, so no password belongs in this job.
    base = Path.home() / '.local/share/goofish-wda-builder'
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gui-', dir=base) as temporary:
        directory = Path(temporary)
        label = 'io.goofish.wda.' + uuid.uuid4().hex
        done = directory / 'done.json'
        plist = directory / 'job.plist'
        plist.write_bytes(plistlib.dumps({
            'Label': label,
            'ProgramArguments': ['/usr/bin/python3', str(Path(__file__).resolve()), '--worker', str(done), *arguments],
            'RunAtLoad': True,
            'LimitLoadToSessionType': 'Aqua',
            'StandardOutPath': str(directory / 'stdout'),
            'StandardErrorPath': str(directory / 'stderr'),
        }))
        domain = 'gui/' + str(os.getuid())
        try:
            run(['launchctl', 'bootstrap', domain, str(plist)], timeout=20)
            deadline = time.monotonic() + 650
            while not done.exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError('Remote WDA GUI build timed out; check desktop login/keychain access')
                time.sleep(.3)
            result = json.loads(done.read_text())
            if not result.get('ok'):
                raise RuntimeError(result.get('error') or 'Remote GUI build failed')
            return result['result']
        finally:
            subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True, timeout=20)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--worker']:
        target = Path(sys.argv[2])
        try:
            result = {'ok': True, 'result': build(*sys.argv[3:])}
        except Exception as error:
            result = {'ok': False, 'error': str(error)}
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(result))
        os.replace(temporary, target)
    else:
        try:
            print(json.dumps(gui_build(sys.argv[1:])))
        except Exception as error:
            print(str(error), file=sys.stderr)
            sys.exit(1)
