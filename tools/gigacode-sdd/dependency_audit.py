"""Operation-scoped configuration checks, distinct from repository inventory.

Neither a filename nor a model's 'test' label proves network isolation. Unbound
configs remain evidence; explicit build inputs are checked before execution.
"""
from __future__ import annotations
import glob
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from policy import audit_dependency_sources, _hosts, _url_problem


class DependencyConfigurationError(RuntimeError):
    """Repair this candidate; do not stop independent candidates/tasks."""
    def __init__(self, report, path):
        self.report, self.path = report, str(path)
        super().__init__('Dependency configuration needs repair; report: ' + self.path)


def assess(root, hosts, *, contours=('.',), operations=()):
    root = Path(root).resolve()
    active, pending, visited, limitations, explicit = {}, [], set(), [], {}
    operation_findings = []
    allowed_hosts = _hosts(hosts)

    def normalize(path):
        path = Path(path)
        return Path(os.path.abspath(path if path.is_absolute() else root / path))

    def select(path, reason):
        path = normalize(path)
        label = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        active.setdefault(label, set()).add(reason)
        return path

    def project(path, reason):
        path = normalize(path)
        if not path.is_relative_to(root):
            limitations.append('Project reference outside captured workspace')
            return
        if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(root)):
            explicit[path / 'package.json'] = 'package'
            select(path / 'package.json', 'symlink-project')
            return
        pending.append((path, reason))

    for contour in contours:
        project(contour, 'configured-contour')
    for op_index, op in enumerate(operations):
        cwd = normalize(op.get('cwd', '.'))
        project(cwd, 'operation-cwd')
        argv = op.get('argv', [])
        executable = Path(argv[0]).name.lower().removesuffix('.cmd').removesuffix('.exe') if argv else ''
        if executable in ('mvn', 'mvnw'):
            options = {'-s': 'xml', '--settings': 'xml', '-gs': 'xml', '--global-settings': 'xml',
                       '-f': 'xml', '--file': 'xml'}
        elif executable in ('npm', 'npx', 'pnpm', 'yarn'):
            options = {'--userconfig': 'npm'}
        elif executable.startswith('pip') or (executable.startswith('python') and argv[1:3] == ['-m', 'pip']):
            options = {'-r': 'pip', '--requirement': 'pip', '-c': 'pip', '--constraint': 'pip'}
        else:
            options = {}
        url_options = ({'--registry'} if executable in ('npm', 'npx', 'pnpm', 'yarn') else
                       {'-i', '--index-url', '--extra-index-url', '--find-links'} if any(k in options for k in ('-r',)) else set())
        def operation_url(value, location):
            code, host = _url_problem(value, allowed_hosts, require_https=True)
            if code:
                operation_findings.append({'path': 'operation:' + str(op_index), 'location': location,
                    'code': code, 'host': host, 'role': 'build-input', 'reasons': ['operation-override']})
        for key in ('NPM_CONFIG_REGISTRY', 'npm_config_registry', 'PIP_INDEX_URL', 'PIP_EXTRA_INDEX_URL', 'PIP_FIND_LINKS'):
            if op.get('env', {}).get(key):
                operation_url(op['env'][key], key)
        for i, arg in enumerate(argv):
            key, separator, value = arg.partition('=')
            if key in url_options:
                operation_url(value if separator else argv[i+1] if i+1 < len(argv) else '', key)
            if key in options:
                value = value if separator else argv[i+1] if i+1 < len(argv) else ''
                if value:
                    path = select(Path(value) if Path(value).is_absolute() else cwd / value, 'explicit-operation-config')
                    explicit[path] = options[key]
                    if key in ('-f', '--file'):
                        project(path.parent, 'explicit-maven-project')
        for key, kind in (('NPM_CONFIG_USERCONFIG', 'npm'), ('npm_config_userconfig', 'npm'),
                          ('PIP_CONFIG_FILE', 'pip')):
            value = op.get('env', {}).get(key)
            if value and value != os.devnull:
                path = select(Path(value) if Path(value).is_absolute() else cwd / value, 'operation-environment')
                explicit[path] = kind
        if argv and Path(argv[0]).name.startswith(('python', 'node', 'sh', 'bash')):
            limitations.append('Script child commands require runtime evidence; not inferred from source strings')

    while pending:
        folder, reason = pending.pop()
        if folder in visited or not folder.is_dir():
            continue
        visited.add(folder)
        ancestor = folder
        while ancestor.is_relative_to(root):
            select(ancestor / '.npmrc', 'inherited-npm-config')
            if ancestor == root:
                break
            ancestor = ancestor.parent
        for path in folder.iterdir():
            if path.name in ('package.json', 'package-lock.json', 'npm-shrinkwrap.json', 'pom.xml', 'settings.xml') or re.fullmatch(r'requirements(?:[.-][\w.-]+)?\.txt', path.name) or path.name.endswith('-settings.xml'):
                select(path, reason)
        pom = folder / 'pom.xml'
        if pom.is_file() and not pom.is_symlink():
            try:
                tree = ET.parse(pom)
                for element in tree.iter():
                    if element.tag.rsplit('}', 1)[-1] == 'modules':
                        for child in element:
                            if child.text:
                                project(folder / child.text.strip(), 'maven-module')
            except (ET.ParseError, OSError):
                pass  # Reported by inventory.
        manifest = folder / 'package.json'
        if manifest.is_file() and not manifest.is_symlink():
            try:
                data = json.loads(manifest.read_text())
                workspaces = data.get('workspaces', [])
                if isinstance(workspaces, dict):
                    workspaces = workspaces.get('packages', [])
                if isinstance(workspaces, list):
                    for pattern in workspaces:
                        if isinstance(pattern, str) and not Path(pattern).is_absolute() and '..' not in Path(pattern).parts:
                            for path in glob.glob(str(folder / pattern)):
                                project(path, 'npm-workspace')
                for section in ('dependencies', 'devDependencies', 'optionalDependencies'):
                    values = data.get(section, {})
                    if isinstance(values, dict):
                        for value in values.values():
                            if isinstance(value, str) and value.startswith(('file:', 'link:')):
                                project(folder / value.split(':', 1)[1], 'local-package')
            except (ValueError, OSError, AttributeError):
                pass

    inventory = []
    findings = audit_dependency_sources(root, hosts, extra_configs=explicit, inventory=inventory)
    blocking, observations = list(operation_findings), []
    for finding in findings:
        reasons = active.get(finding['path'])
        (blocking if reasons else observations).append({**finding,
            'role': 'build-input' if reasons else 'unbound-config', 'reasons': sorted(reasons or [])})
    return {'schema': 1, 'blocking': blocking, 'observations': observations,
            'unbound_configs': [item for item in inventory if item['path'] not in active],
            'limitations': sorted(set(limitations)), 'network_isolation_proven': False,
            'active_configs': {k: sorted(v) for k, v in sorted(active.items())}}


def preserve_generated_private_configs(runner, work, report):
    """Keep generated private test inputs outside product patches, without loss.

    Only untracked, unbound dependency configs qualify. Tracked files, active
    configs, arbitrary secrets and symlinks retain their existing protections.
    This is storage classification, never an execution/network exemption.
    """
    import shutil
    from workspace import _secret, _tracked, _safe_path
    from controller import save
    work = Path(work).resolve()
    tracked = set(_tracked(work))
    candidates = [item['path'] for item in report.get('unbound_configs', [])
                  if item['path'] not in tracked and _secret(item['path'])]
    if not candidates:
        return []
    folder = runner.logdir('private-test-inputs')
    folder.mkdir(parents=True, mode=0o700)
    records = []
    for name in sorted(set(candidates)):
        source = _safe_path(work, name)
        if not source.is_file() or any(p.is_symlink() for p in (source, *source.parents)):
            continue
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Save a durable copy and receipt before removing only this generated
        # runtime input. Never include its contents in diagnostics or patches.
        with source.open('rb') as src, target.open('xb') as dst:
            os.chmod(target, 0o600)
            shutil.copyfileobj(src, dst)
            dst.flush(); os.fsync(dst.fileno())
        records.append({'source': str(source), 'preserved': str(target),
                        'reason': 'untracked private config not referenced by build inputs'})
        save(folder / 'receipt.json', {'files': records})
        source.unlink()
    return records
