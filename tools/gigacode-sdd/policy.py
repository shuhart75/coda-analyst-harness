"""Explicit project permissions and Nexus configuration checks, not an OS sandbox.

Tool aliases vary between GigaCode releases. A prepared allow list is a candidate
configuration; only an actual headless preflight proves it usable on a host.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


ENFORCEMENT = "instructions-and-config-audit"
READ_ALLOW = (
    "Read", "Bash", "run_shell_command", "read_file", "list_directory",
    "glob", "grep_search", "Skill", "Agent", "skill", "agent",
    "create_sub_session", "send_message", "list_agents", "task_stop",
)
WRITE_ALLOW = ("Edit", "Write", "write_file", "edit", "replace")
EXCLUDED_DIRS = {".git", "node_modules", ".venv", "venv", "target", "dist",
                 "__pycache__", "m2repo", "npm-cache", ".cache"}
URL_RE = re.compile(r"(?:[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s\"'<>]+")


def _role(role):
    if role not in {"execute", "review"}:
        raise ValueError("role must be execute or review")


def _settings(path: Path):
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("GigaCode settings cannot be a symlink")
    if not path.exists():
        return None, {}
    before = path.read_bytes()
    try:
        data = json.loads(before.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise ValueError("Invalid GigaCode settings JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("permissions", {}), dict):
        raise ValueError("Invalid GigaCode permissions object")
    for kind in ("allow", "deny", "ask"):
        rules = data.get("permissions", {}).get(kind, [])
        if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
            raise ValueError("GigaCode permission rules must be lists of strings")
    return before, data


def _no_symlink_ancestors(path: Path):
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError("Workspace settings path cannot traverse a symlink")


def _restriction_report(workspace: Path, local):
    reports = []
    here = workspace
    while True:
        path = here / ".gigacode" / "settings.json"
        data = local if here == workspace else _settings(path)[1]
        permissions = data.get("permissions", {})
        if permissions.get("ask") or permissions.get("deny"):
            # Do not print raw rules: a command-scoped rule can contain a secret.
            reports.append({"path": str(path), "ask_count": len(permissions.get("ask", [])),
                            "deny_count": len(permissions.get("deny", []))})
        if (here / ".git").exists() or here.parent == here:
            break
        here = here.parent
    return reports


def prepare_permissions(workspace: Path, role="execute", extra_allow=()):
    """Merge candidate allow rules into one project/contour, preserving deny/ask.

    Existing global restrictions, workspace trust, and shell security policies
    are never removed. Review callers must additionally use approval-mode plan;
    this function cannot impose filesystem read-only access on shell commands.
    """
    _role(role)
    workspace = Path(os.path.abspath(os.path.expanduser(str(workspace))))
    _no_symlink_ancestors(workspace)
    if not workspace.is_dir():
        raise ValueError("Permission workspace must exist")
    if isinstance(extra_allow, str) or not all(isinstance(r, str) and r.strip() for r in extra_allow):
        raise ValueError("extra_allow must contain explicit rule strings")
    desired = tuple(dict.fromkeys(READ_ALLOW + (WRITE_ALLOW if role == "execute" else ()) + tuple(extra_allow)))
    path = workspace / ".gigacode" / "settings.json"
    before, data = _settings(path)
    restrictions = _restriction_report(workspace, data)
    permissions = data.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    added = [rule for rule in desired if rule not in allow]
    if added:
        allow.extend(added)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        _no_symlink_ancestors(path.parent)
        if before is not None:
            backup = path.with_name("settings.json.before-sdd-runner")
            if backup.is_symlink():
                raise ValueError("Settings backup cannot be a symlink")
            try:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(before)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if path.is_symlink() or (path.read_bytes() if path.exists() else None) != before:
                raise ValueError("Settings changed concurrently; retry preparation")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return {"path": str(path), "added": added, "role": role,
            "restrictions": restrictions, "requires_preflight": True,
            "review_requires_plan_mode": role == "review", "enforcement": ENFORCEMENT}


def _hosts(values):
    if isinstance(values, str) or not isinstance(values, (list, tuple)) or not values:
        raise ValueError("Nexus host allowlist must be a nonempty list")
    result = set()
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
            raise ValueError("Nexus allowlist requires exact host names, without wildcard or URL")
        if ".." in value:
            raise ValueError("Invalid Nexus host name")
        result.add(value.lower())
    return result


def _url_problem(value, hosts, require_https=False):
    if not isinstance(value, str) or not value.strip():
        return "invalid-url", None
    raw = value.strip()
    if "${" in raw or "%('" in raw or "$env:" in raw:
        return "unresolved-download-url", None
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        if parsed.username is not None or parsed.password is not None:
            return "credentials-in-download-url", host
    except ValueError:
        return "invalid-url", None
    if not parsed.scheme and raw.startswith("//"):
        return "insecure-download-url", host
    if parsed.scheme in {"file", "link", "workspace"} or not parsed.scheme:
        return ("invalid-url", None) if require_https else (None, None)
    if not host or host.lower() not in hosts:
        return "non-nexus-download", host
    if parsed.scheme != "https":
        return "insecure-download-url", host
    return None, host


def nexus_environment(policy: dict, base_env=None):
    """Return an env with explicit Nexus registries; never manufacture credentials.

    This does not intercept traffic. Project configuration, child tools, Maven
    settings and redirects require separate audits and runtime enforcement.
    """
    hosts = _hosts(policy.get("nexus_hosts", policy.get("hosts")))
    env = dict(os.environ if base_env is None else base_env)
    for key, name in (("npm_registry", "NPM_CONFIG_REGISTRY"), ("pip_index_url", "PIP_INDEX_URL")):
        configured = policy.get(key)
        existing = env.get(name) or env.get(name.lower())
        value = configured if configured is not None else existing
        if value:
            problem, _ = _url_problem(value, hosts, require_https=True)
            if problem:
                raise ValueError(f"{name} must be an explicit HTTPS Nexus URL without credentials")
            env.pop(name.lower(), None)
            env[name] = value
    # A secondary index may silently become a public fallback. Even Nexus extra
    # indexes are intentionally disabled; use the selected Nexus group endpoint.
    env.pop("pip_extra_index_url", None)
    env["PIP_EXTRA_INDEX_URL"] = ""
    # Disable user/global pip config so its extra-index/find-links entries cannot
    # reintroduce a fallback behind the selected explicit group registry.
    env.pop("pip_config_file", None)
    env["PIP_CONFIG_FILE"] = os.devnull
    env.pop("pip_find_links", None)
    env["PIP_FIND_LINKS"] = ""
    # Missing explicit endpoints must not silently select the public defaults.
    # This still is not a network sandbox: subprocesses can override settings.
    if not env.get("PIP_INDEX_URL"):
        env["PIP_NO_INDEX"] = "1"
    if not env.get("NPM_CONFIG_REGISTRY"):
        env.pop("npm_config_offline", None)
        env["NPM_CONFIG_OFFLINE"] = "true"
    return env


def audit_dependency_sources(root, nexus_hosts):
    """Inspect dependency download URLs only, not documentation or XML namespaces.

    Findings intentionally omit complete URLs, which may contain credentials.
    Installation scripts, runtime redirects and user-global configs are beyond
    this static audit; an empty list is not proof of network isolation.
    """
    root = Path(root).resolve()
    hosts = _hosts(nexus_hosts)
    findings = []

    def add(path, location, code, host=None):
        findings.append({"path": str(path.relative_to(root)), "location": str(location),
                         "code": code, "host": host})

    def check(path, location, value, required=False):
        code, host = _url_problem(value, hosts, required)
        if code:
            add(path, location, code, host)

    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            kind = ("package" if name == "package.json" else
                    "lock" if name in {"package-lock.json", "npm-shrinkwrap.json"} else
                    "npm" if name == ".npmrc" else
                    "pip" if re.fullmatch(r"requirements(?:[.-][\w.-]+)?\.txt", name) else
                    "xml" if name in {"pom.xml", "settings.xml"} or name.endswith("-settings.xml") else None)
            if kind is None:
                continue
            path = Path(directory) / name
            if path.is_symlink():
                add(path, "file", "symlink-config-not-audited")
                continue
            try:
                if path.stat().st_size > 32 * 1024 * 1024:
                    add(path, "file", "config-too-large")
                    continue
                text = path.read_text(encoding="utf-8-sig")
                if kind == "package":
                    data = json.loads(text)
                    for section in ("dependencies", "devDependencies", "optionalDependencies",
                                    "peerDependencies", "overrides", "resolutions"):
                        def dependency_values(obj, location):
                            if isinstance(obj, dict):
                                for key, value in obj.items():
                                    dependency_values(value, location + "." + key)
                            elif isinstance(obj, str):
                                if "://" in obj or obj.startswith("//"):
                                    check(path, location, obj)
                                elif obj.startswith(("github:", "gitlab:", "bitbucket:", "git@")):
                                    add(path, location, "non-nexus-download")
                                elif re.fullmatch(r"[\w.-]+/[\w.-]+(?:#.*)?", obj):
                                    add(path, location, "non-nexus-download")
                        dependency_values(data.get(section, {}), section)
                elif kind == "lock":
                    data = json.loads(text)
                    def walk(obj, location="$"):
                        if isinstance(obj, dict):
                            for key, value in obj.items():
                                if key == "resolved":
                                    check(path, location + ".resolved", value)
                                else:
                                    walk(value, location + "." + key)
                        elif isinstance(obj, list):
                            for index, value in enumerate(obj):
                                walk(value, f"{location}[{index}]")
                    walk(data)
                elif kind == "npm":
                    for index, line in enumerate(text.splitlines(), 1):
                        stripped = line.strip()
                        if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
                            continue
                        key, value = stripped.split("=", 1)
                        if key.strip() == "registry" or key.strip().endswith(":registry"):
                            check(path, index, value.strip(), True)
                elif kind == "pip":
                    for index, line in enumerate(text.splitlines(), 1):
                        stripped = line.strip()
                        if stripped.startswith("#"):
                            continue
                        for value in URL_RE.findall(stripped):
                            check(path, index, value)
                        if re.match(r"(?:--extra-index-url|--index-url|-i|--find-links|-f)(?:\s|=)", stripped) and not URL_RE.search(stripped):
                            add(path, index, "unresolved-download-url")
                elif kind == "xml":
                    tree = ET.fromstring(text)
                    for element in tree.iter():
                        tag = element.tag.rsplit("}", 1)[-1]
                        if tag not in {"repository", "pluginRepository", "mirror"}:
                            continue
                        for child in element:
                            if child.tag.rsplit("}", 1)[-1] == "url":
                                check(path, tag + "/url", child.text or "", True)
            except (ValueError, UnicodeError, OSError, ET.ParseError):
                add(path, "file", "invalid-dependency-config")
    return findings


def make_policy_prompt(nexus_hosts, role):
    _role(role)
    hosts = ", ".join(sorted(_hosts(nexus_hosts)))
    return f"""Обязательные ограничения всех сессий и вложенных агентов.
Скачивания инструментов, пакетов, образов и транзитивных зависимостей разрешены
только через HTTPS Nexus с точными хостами: {hosts}. Публичные сайты, CDN,
GitHub Releases, PyPI, npmjs, Maven Central и резервные внешние источники запрещены.
Это относится к npx, codegen, postinstall, pip, Maven и перенаправлениям.
При отсутствии артефакта или доступа зафиксируй блокер с координатами зависимости.
Не обходи запрет другим инструментом. Секреты не выводи в отчёт или Git.
Работай по существующим SDD-инструкциям текущего контура и неизменяемому входному
пакету. Согласованные story/tasks сохраняются; технические шаги добавляй внутри
них. Не ослабляй требования и проверки ради успешного результата.
Записывай только в назначенной рабочей копии и разрешённых путях задачи.
Зарегистрированный code-клон и общие среды не изменяй; push запрещён.
Не снимай deny/ask, не меняй политики и не обходи shell security policy.
Отказ обязательного инструмента означает блокер конфигурации, а не повод
повторять запрещённое действие через другой синтаксис.
{'Роль: независимое ревью. Не меняй исходники, SDD, настройки или критерии. Для проверок используй отдельные временные данные; результаты передай контроллеру.' if role == 'review' else 'Роль: исполнитель. Соблюдай выделенную область файлов и фиксируй выполненные проверки и остаток задания.'}
Эти инструкции и проверка конфигурации не являются сетевой или файловой
песочницей ОС. Не заявляй техническую изоляцию без соответствующих доказательств.
"""
