import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app_monitor.config import settings
from app_monitor.quotes import QuoteImageParser,QuoteService
from app_monitor.swift_runtime import run_swift


def test_sdk_override_controls_both_compiler_argument_and_child_environment(tmp_path,monkeypatch):
    sdk=tmp_path/'matching sdk';sdk.mkdir()
    monkeypatch.setenv('SDKROOT','/inherited/incompatible.sdk')
    calls=[]
    def fake(command,**kwargs):calls.append((command,kwargs));return SimpleNamespace(returncode=0,stdout='ok')
    monkeypatch.setattr('app_monitor.swift_runtime.subprocess.run',fake)
    run_swift(Path('/scripts/ocr.swift'),['/images/quote with spaces.png'],sdk=str(sdk),timeout=15)
    command,kwargs=calls[0]
    assert command==['swift','-sdk',str(sdk),'/scripts/ocr.swift','/images/quote with spaces.png']
    assert kwargs['env']['SDKROOT']==str(sdk) and kwargs['timeout']==15
    assert os.environ['SDKROOT']=='/inherited/incompatible.sdk'


def test_unconfigured_sdk_retains_existing_toolchain_behavior(monkeypatch):
    calls=[]
    monkeypatch.setattr('app_monitor.swift_runtime.subprocess.run',lambda command,**kwargs:calls.append((command,kwargs)))
    run_swift(Path('/scripts/ocr.swift'),['/images/quote.png'])
    assert calls[0][0]==['swift','/scripts/ocr.swift','/images/quote.png']
    assert calls[0][1]['env'] is None


def test_missing_explicit_sdk_reports_actionable_configuration_error(tmp_path):
    with pytest.raises(RuntimeError,match='APP_MONITOR_SWIFT_SDK'):
        run_swift(Path('/scripts/ocr.swift'),[],sdk=str(tmp_path/'missing.sdk'))


def test_quote_ocr_and_quote_service_use_selected_sdk(database,tmp_path,monkeypatch):
    sdk=tmp_path/'sdk';sdk.mkdir()
    service=QuoteService(database,replace(settings,swift_sdk=str(sdk)))
    assert service.parser.sdk==str(sdk)
    image=tmp_path/'quote.png';Image.new('RGB',(50,50),'white').save(image)
    calls=[]
    def recognize(command,**kwargs):
        calls.append((command,kwargs))
        return SimpleNamespace(returncode=0,stdout='0\t0.1\t0.5\t0.2\t0.1\t17\n')
    monkeypatch.setattr('app_monitor.swift_runtime.subprocess.run',recognize)
    assert QuoteImageParser(sdk=str(sdk))._recognize(image)[0].text=='17'
    assert calls[0][0][1:3]==['-sdk',str(sdk)]
