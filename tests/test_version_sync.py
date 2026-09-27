from __future__ import annotations

import tomllib
from pathlib import Path

from arara_factory.version import __version__
from arara_factory import __version__ as package_version


def test_version_is_synchronized() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    installer = (root / "installer" / "arara_factory.iss").read_text(encoding="utf-8")

    assert project["project"]["version"] == __version__
    assert package_version == __version__
    assert f'#define MyAppVersion "{__version__}"' in installer


def test_rebranded_package_keeps_legacy_python_entry_points() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["name"] == "verticliv"
    scripts = project["project"]["scripts"]
    for name in ("verticliv", "verticliv-gui", "verticliv-batch", "arara-factory"):
        assert scripts[name] == "arara_factory.entry:main"
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/arara_factory"
    ]


def test_rebranded_installer_upgrades_existing_installation() -> None:
    root = Path(__file__).resolve().parents[1]
    installer = (root / "installer" / "arara_factory.iss").read_text(encoding="utf-8")

    assert '#define MyAppName "Verticliv"' in installer
    assert '#define MyAppExeName "Verticliv.exe"' in installer
    assert "AppId={{F13BF67D-816D-45EA-8C72-75FA431AFA7B}" in installer
    assert "UsePreviousAppDir=yes" in installer
    assert "DefaultDirName={localappdata}\\Programs\\Verticliv" in installer
    assert "OutputBaseFilename=Verticliv-Setup" in installer
    assert 'Type: files; Name: "{app}\\ARARA-Factory.exe"' in installer
    assert 'Type: files; Name: "{autodesktop}\\ARARA Factory.lnk"' in installer
    assert "Type: filesandordirs" not in installer


def test_release_keeps_legacy_installer_alias() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "build-windows.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "'--name', 'Verticliv'" in workflow
    assert "name: Verticliv-Installer" in workflow
    assert "name: Verticliv-Portable" in workflow
    assert "$asset = 'installer-output/Verticliv-Setup.exe'" in workflow
    assert "$legacyAsset = 'installer-output/ARARA-Factory-Setup.exe'" in workflow
    assert "Copy-Item -LiteralPath $asset -Destination $legacyAsset" in workflow
    assert "gh release create $tag $asset $legacyAsset" in workflow
    assert "gh release upload $tag $asset $legacyAsset" in workflow
