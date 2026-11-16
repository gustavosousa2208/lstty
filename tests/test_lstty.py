import hashlib
import importlib.machinery
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LSTTY_PATH = REPO_ROOT / "lstty.py"


def load_lstty():
    loader = importlib.machinery.SourceFileLoader("lstty_standalone", str(LSTTY_PATH))
    spec = importlib.util.spec_from_loader("lstty_standalone", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_version_flag_reads_dev_in_repo(capsys):
    mod = load_lstty()
    rc = mod.main(["--version"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "lstty dev"


def test_update_refuses_inside_git_repo(tmp_path, monkeypatch, capsys):
    mod = load_lstty()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(mod, "INSTALL_ROOT", tmp_path)
    rc = mod.update_self(SimpleNamespace(force=False, check=False, quiet=False))
    assert rc == 1
    assert "git pull" in capsys.readouterr().err


def test_verify_sha256_accepts_match_and_rejects_mismatch(tmp_path):
    mod = load_lstty()
    payload = tmp_path / "lstty-windows-x86_64.zip"
    payload.write_bytes(b"hello lstty")
    good = hashlib.sha256(b"hello lstty").hexdigest()
    mod._verify_sha256(payload, f"{good}  lstty-windows-x86_64.zip\n", "lstty-windows-x86_64.zip")

    bad = "0" * 64
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        mod._verify_sha256(payload, f"{bad}  lstty-windows-x86_64.zip\n", "lstty-windows-x86_64.zip")


def test_install_extracted_tree_copies_files(tmp_path, monkeypatch):
    mod = load_lstty()
    install_root = tmp_path / "install"
    install_root.mkdir()
    monkeypatch.setattr(mod, "INSTALL_ROOT", install_root)
    monkeypatch.setattr(mod, "VERSION_FILE", install_root / "VERSION")

    extracted = tmp_path / "extracted"
    (extracted / "benchlib").mkdir(parents=True)
    (extracted / "lstty.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (extracted / "benchlib" / "__init__.py").write_text("", encoding="utf-8")
    (extracted / "benchlib" / "probelock.py").write_text("# lock\n", encoding="utf-8")
    (extracted / "VERSION").write_text("v0.1.0", encoding="utf-8")
    exe_name = "lstty.exe" if sys.platform == "win32" else "lstty"
    (extracted / exe_name).write_bytes(b"binary")

    mod._install_extracted_tree(extracted, "v0.1.0")

    assert (install_root / "lstty.py").exists()
    assert (install_root / "benchlib" / "probelock.py").exists()
    assert (install_root / "VERSION").read_text(encoding="utf-8") == "v0.1.0"
    assert (install_root / exe_name).read_bytes() == b"binary"


def test_maybe_print_update_notice_emits_stderr_when_newer(tmp_path, monkeypatch, capsys):
    mod = load_lstty()
    install_root = tmp_path / "install"
    install_root.mkdir()
    version_file = install_root / "VERSION"
    version_file.write_text("v0.1.0", encoding="utf-8")
    cache_file = tmp_path / "cache" / "update-check.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(
        json.dumps({"checked_at": int(time.time()), "latest": "v0.2.0"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(mod, "INSTALL_ROOT", install_root)
    monkeypatch.setattr(mod, "VERSION_FILE", version_file)
    monkeypatch.setattr(mod, "_update_cache_file", lambda: cache_file)
    monkeypatch.delenv("LSTTY_NO_UPDATE_CHECK", raising=False)

    mod.maybe_print_update_notice()
    err = capsys.readouterr().err
    assert "v0.2.0" in err
    assert "lstty update" in err
