"""配置加载与 Prompt 读取。业务代码只依赖 dict，不硬编码服务商细节。"""
import os
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except ImportError:  # 允许无 pyyaml 时仍可导入模块（mock 联调不需要读 yaml）
    yaml = None

from .env import project_root

_HERE = project_root()  # 项目根（打包后为 exe 所在目录）


def load_config(path: str = "config/config.yaml", overrides: Dict[str, Any] = None) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = _HERE / p
    cfg = _read_yaml(p)
    # 同目录的 config.local.yaml 覆盖主配置：应用界面改的模型、并发写在这里，
    # 升级时主配置被替换也不会冲掉用户设置；删掉该文件即恢复默认。
    local = p.with_name("config.local.yaml")
    if p.name != "config.local.yaml" and local.exists():
        _deep_update(cfg, _read_yaml(local))
    if overrides:
        _deep_update(cfg, overrides)
    return cfg


def _read_yaml(p: Path) -> dict:
    text = p.read_text(encoding="utf-8")
    if yaml is not None:
        return yaml.safe_load(text) or {}
    return _mini_yaml(text)  # 无 pyyaml 时的最小解析（覆盖本项目配置子集）


def _deep_update(base: dict, patch: dict) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def _split_top(s: str) -> list:
    """按顶层逗号切分（忽略 {} [] 引号内部）。"""
    out, buf, depth, quote = [], "", 0, None
    for ch in s:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf += ch
        elif ch in "{[":
            depth += 1
            buf += ch
        elif ch in "}]":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf)
    return out


def _parse_value(s: str):
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        d = {}
        for part in _split_top(inner):
            k, _, v = part.partition(":")
            d[k.strip()] = _parse_value(v)
        return d
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_parse_value(x) for x in _split_top(inner)] if inner else []
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s in ("null", "~", ""):
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("\"", "'"):
        return s[1:-1]
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


def _mini_yaml(text: str) -> dict:
    """最小 YAML 解析：支持嵌套映射、内联 dict/list、标量与注释。"""
    rows = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        rows.append((indent, line.strip()))

    root = {}
    stack = [(-1, root)]
    for indent, content in rows:
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if content.endswith(":"):
            key = content[:-1].strip()
            child = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            key, _, val = content.partition(":")
            parent[key.strip()] = _parse_value(val)
    return root


# 提示词查找顺序：用户本地覆盖 → 内置。缓存键含 mtime，改完文件立刻生效。
_PROMPT_CACHE: Dict[str, tuple] = {}
_OVERRIDE_DIR = "config/prompts.local"


def prompt_paths(name: str, prompts_dir: str = "config/prompts") -> list:
    """返回该提示词的候选路径，按优先级从高到低。

    用户改提示词不动内置文件：把同名 txt 放进 config/prompts.local/ 即可。
    这样升级覆盖内置提示词时，本地改动不会被冲掉；出问题时删掉覆盖文件就回到默认。
    """
    out = []
    for d in (_OVERRIDE_DIR, prompts_dir):
        p = Path(d)
        if not p.is_absolute():
            p = _HERE / p
        out.append(p / f"{name}.txt")
    return out


def read_prompt(name: str, prompts_dir: str = "config/prompts") -> str:
    for p in prompt_paths(name, prompts_dir):
        if p.exists():
            st = p.stat()
            key = str(p)
            hit = _PROMPT_CACHE.get(key)
            if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
                return hit[2]
            text = p.read_text(encoding="utf-8")
            _PROMPT_CACHE[key] = (st.st_mtime, st.st_size, text)
            return text
    tried = "、".join(str(p) for p in prompt_paths(name, prompts_dir))
    raise FileNotFoundError(f"提示词缺失：{name}.txt（已查找 {tried}）")


def prompt_source(name: str, prompts_dir: str = "config/prompts") -> str:
    """返回 "local" / "builtin" / "missing"，用于在结果里标明用的是哪一份。"""
    paths = prompt_paths(name, prompts_dir)
    for tag, p in zip(("local", "builtin"), paths):
        if p.exists():
            return tag
    return "missing"


def prompt_sources(prompts_dir: str = "config/prompts") -> Dict[str, str]:
    names = ("asr", "sed", "music", "vl", "merge", "calibrate")
    return {n: prompt_source(n, prompts_dir) for n in names}


def resolve_path(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else _HERE / p
