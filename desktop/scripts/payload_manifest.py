"""安装清单：安装程序只写入这些文件，卸载程序只删除这些文件和目录（不对安装目录做递归删除）。

这样即使用户把程序装进了一个已有其他文件的目录，卸载也只会移除 GUI Agent 自己的文件。
"""
from __future__ import annotations

from pathlib import Path, PureWindowsPath

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent"
DATA_DIR_NAME = "GUI Agent"          # %APPDATA%\GUI Agent


def collect(stage: Path) -> tuple[list[str], list[str]]:
    """返回 (文件, 目录)，均为相对 stage 的 Windows 风格路径；目录按“先子后父”排序。

    只列出真正装有文件的目录（空目录不会被安装，也就不需要卸载）。
    """
    files, dirs = [], set()
    for path in sorted(stage.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlinks are not allowed in stage: {path}")
        if path.is_file():
            parts = path.relative_to(stage).parts
            files.append(str(PureWindowsPath(*parts)))
            for i in range(1, len(parts)):
                dirs.add(str(PureWindowsPath(*parts[:i])))
        elif not path.is_dir():
            raise ValueError(f"unsupported entry in stage: {path}")
    return files, sorted(dirs, key=lambda d: (-d.count("\\"), d))


def _quote(value: str) -> str:
    if '"' in value or "\n" in value or "\r" in value:
        raise ValueError(f"unsafe path for NSIS: {value!r}")
    return value.replace("$", "$$")   # NSIS 里 $ 是变量前缀


def nsis_install(stage: Path, files: list[str], dirs: list[str]) -> str:
    lines = []
    current = None
    for rel in files:
        parent = str(PureWindowsPath(rel).parent)
        target = "$INSTDIR" if parent == "." else f"$INSTDIR\\{_quote(parent)}"
        if target != current:
            lines.append(f'SetOutPath "{target}"')
            current = target
        source = stage.joinpath(*PureWindowsPath(rel).parts)
        lines.append(f'File "/oname={_quote(PureWindowsPath(rel).name)}" "{_quote(str(source))}"')
    lines.append('SetOutPath "$INSTDIR"')
    return "\n".join(lines) + "\n"


def nsis_uninstall(files: list[str], dirs: list[str]) -> str:
    lines = [f'Delete "$INSTDIR\\{_quote(rel)}"' for rel in files]
    # RMDir 不带 /r：目录里若有用户自己放的文件就保留
    lines += [f'RMDir "$INSTDIR\\{_quote(rel)}"' for rel in dirs]
    return "\n".join(lines) + "\n"


def size_kb(stage: Path, files: list[str]) -> int:
    return sum(stage.joinpath(*PureWindowsPath(f).parts).stat().st_size for f in files) // 1024 + 1
