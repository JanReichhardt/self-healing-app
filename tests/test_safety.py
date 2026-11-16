import asyncio
import json
from pathlib import Path

import pytest
from orchestrator.agent import repair, SOURCE, BUG, FIX
from orchestrator.engine import Engine, digest_tree


def test_deterministic_fixture_only(tmp_path):
    f=tmp_path/SOURCE; f.parent.mkdir(parents=True); f.write_text(BUG)
    repair(tmp_path,'deterministic'); assert f.read_text()==FIX
    with pytest.raises(ValueError): repair(tmp_path,'deterministic')


def test_digest_detects_content_and_path_changes(tmp_path):
    f=tmp_path/'a'; f.write_text('one'); a=digest_tree(tmp_path)
    f.write_text('two'); assert digest_tree(tmp_path)!=a
    b=digest_tree(tmp_path); f.rename(tmp_path/'b'); assert digest_tree(tmp_path)!=b


def test_digest_rejects_symlink(tmp_path):
    (tmp_path/'a').symlink_to('/etc/hosts')
    with pytest.raises(ValueError): digest_tree(tmp_path)


def test_approval_gate_rejects_stale_and_mutated_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_MODE','deterministic')
    e=Engine(tmp_path); e.status='awaiting_approval'; e.incident='a'; e.release=tmp_path/'release'
    e.release.mkdir(); f=e.release/'app'; f.write_text('tested'); e.digest=digest_tree(e.release)
    with pytest.raises(ValueError): asyncio.run(e.approve('old',e.digest))
    with pytest.raises(ValueError): asyncio.run(e.approve('a','wrong'))
    assert e.status=='awaiting_approval' and e.active is None
    f.write_text('tampered')
    with pytest.raises(ValueError): asyncio.run(e.approve('a',e.digest))
    assert e.status=='failed' and e.active is None


def test_reject_does_not_deploy(tmp_path):
    e=Engine(tmp_path); e.status='awaiting_approval'; e.active='baseline'
    asyncio.run(e.reject()); assert e.status=='rejected' and e.active=='baseline'


def test_failed_candidate_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_MODE','deterministic')
    e=Engine(tmp_path); e.status='awaiting_approval'; e.incident='a'
    e.release=tmp_path/'release'; e.release.mkdir(); (e.release/'app').write_text('tested')
    e.digest=digest_tree(e.release); e.active='baseline'; e.candidate='candidate'
    calls=[]
    async def contract(url):
        calls.append(url)
        if len(calls)==2: raise ValueError('post-deploy failure')
    e.contract=contract
    with pytest.raises(ValueError): asyncio.run(e.approve('a',e.digest))
    assert e.active=='baseline' and e.status=='failed' and len(calls)==2


def test_replay_requires_explicit_approval(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_MODE','replay')
    e=Engine(tmp_path); e.status='awaiting_approval'; e.incident='a'; e.digest='simulation'
    asyncio.run(e.approve('a','simulation')); assert e.status=='resolved' and e.active is None
    with pytest.raises(ValueError): asyncio.run(e.approve('a','simulation'))


def test_probe_event_accepts_measurement_timestamp(tmp_path):
    e=Engine(tmp_path)
    event=e.emit("probe", "passed", ok=True, time=123.0)
    assert event["id"]==1 and event["time"]==123.0
    assert json.loads((e.state/"events.jsonl").read_text())["ok"] is True


def test_openhands_adapter_initializes_without_model_call(tmp_path, monkeypatch):
    sdk=pytest.importorskip('openhands.sdk')
    from unittest.mock import patch
    workspace=tmp_path/'workspace'; workspace.mkdir()
    with patch.object(sdk.LocalConversation,'run') as run:
        repair(workspace,'openhands')
        run.assert_called_once()
