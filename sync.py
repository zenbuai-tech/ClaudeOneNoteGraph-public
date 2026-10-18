#!/usr/bin/env python3
"""OneNote -> Markdown mirror. source_agent=claude-code; machine=mac-mini.

OneNote remains authoritative. This process performs GET requests only.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
from urllib import error, parse, request
import uuid

try:
    import fcntl
except ImportError:  # Windows has no fcntl; msvcrt provides the equivalent lock.
    fcntl = None
    import msvcrt
import onenote

ROOT = Path(__file__).resolve().parent
DEFAULT_VAULT = ROOT / 'demo-vault'
MIRROR = 'OneNote_Mirror'
ASSETS = 'attachments'
FORMAT_VERSION = 1


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def guard(path):
    path = Path(os.path.abspath(str(path)))
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise onenote.UserError('Symlink paths are not allowed: ' + path.name)
    return path


def atomic(path, data):
    path = guard(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as f:
            os.chmod(temp, 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def save_json(path, obj):
    atomic(path, (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode())


@contextmanager
def locked(vault):
    vault = guard(vault)
    vault.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir = guard(vault / '.onenote-sync')
    state_dir.mkdir(exist_ok=True, mode=0o700)
    with guard(state_dir / 'run.lock').open('a') as f:
        if fcntl is not None:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise onenote.UserError('A sync is already running.') from None
            yield state_dir
            return
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise onenote.UserError('A sync is already running.') from None
        try:
            yield state_dir
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


class BudgetExceeded(onenote.UserError):
    pass


class Budget:
    def __init__(self, maximum):
        self.maximum, self.used = maximum, 0

    def take(self):
        if self.used >= self.maximum:
            raise BudgetExceeded('Request budget reached; next run resumes pending pages.')
        self.used += 1

    def transport(self, method, url, headers=None, data=None):
        if method != 'GET':
            raise onenote.UserError('Mirror requests must be read-only.')
        self.take()
        return onenote.http(method, url, headers, data)


class Node:
    def __init__(self, tag='root', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []


class Tree(HTMLParser):
    VOID = {'br', 'hr', 'img', 'meta', 'link', 'input', 'source', 'wbr', 'area', 'base', 'embed', 'param', 'col'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack = self.stack[:i]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def safe_link(value):
    value = (value or '').strip()
    p = parse.urlsplit(value)
    if p.scheme.lower() not in ('https', 'http', 'mailto') or p.username or p.password:
        return ''
    return value.replace(' ', '%20').replace('(', '%28').replace(')', '%29').replace('\n', '')


def escape_text(text):
    return re.sub(r'([\\`*_\[\]<>#])', r'\\\1', text)


class Markdown:
    def __init__(self, resource=None):
        self.resource = resource
        self.warnings = []

    def convert(self, content):
        tree = Tree()
        tree.feed(content)
        result = self.render(tree.root)
        result = '\n'.join(line.rstrip() for line in result.splitlines())
        return re.sub(r'\n{3,}', '\n\n', result).strip() + '\n'

    def render(self, node, depth=0):
        if isinstance(node, str):
            return escape_text(re.sub(r'\s+', ' ', node))
        tag, attrs = node.tag, node.attrs
        if tag in ('head', 'script', 'style'):
            return ''
        if tag in ('img', 'object'):
            url = (attrs.get('data-fullres-src') or attrs.get('src')) if tag == 'img' else attrs.get('data')
            label = attrs.get('alt') or attrs.get('data-attachment') or ('Image' if tag == 'img' else 'Attachment')
            local = self.resource(url, label) if self.resource and url else None
            if local:
                return ('!' if tag == 'img' else '') + '[' + escape_text(label) + '](' + local + ')\n'
            self.warnings.append('Media not imported: ' + label[:150])
            return '\n> [Media not imported: ' + escape_text(label) + ']\n'
        if tag in ('svg', 'canvas', 'iframe', 'audio', 'video'):
            self.warnings.append('Unsupported element: ' + tag)
            return '\n> [Unsupported content: ' + tag + ']\n'
        if tag in ('ul', 'ol'):
            lines = []
            count = 0
            for child in node.children:
                if isinstance(child, Node) and child.tag == 'li':
                    count += 1
                    text = ''.join(self.render(c, depth + 1) for c in child.children).strip()
                    bullet = str(count) + '. ' if tag == 'ol' else '- '
                    parts = text.splitlines() or ['']
                    lines.append(bullet + parts[0] + ''.join('\n  ' + p for p in parts[1:]))
            return '\n' + '\n'.join(lines) + '\n\n'
        if tag == 'table':
            rows = []
            def collect(n):
                if isinstance(n, str):
                    return
                if n.tag == 'tr':
                    cells = []
                    for cell in n.children:
                        if isinstance(cell, Node) and cell.tag in ('td', 'th'):
                            if cell.attrs.get('colspan', '1') != '1' or cell.attrs.get('rowspan', '1') != '1':
                                self.warnings.append('Merged table cells flattened; compare with source.')
                            cells.append(''.join(self.render(c) for c in cell.children).strip().replace('|', '\\|').replace('\n', '<br>'))
                    rows.append(cells)
                else:
                    for c in n.children:
                        collect(c)
            collect(node)
            if not rows:
                return ''
            width = max(len(r) for r in rows)
            rows = [r + [''] * (width - len(r)) for r in rows]
            rows.insert(1, ['---'] * width)
            return '\n\n' + '\n'.join('| ' + ' | '.join(r) + ' |' for r in rows) + '\n\n'
        body = ''.join(self.render(child, depth) for child in node.children)
        if tag in ('b', 'strong'):
            return '**' + body.strip() + '**'
        if tag in ('i', 'em'):
            return '*' + body.strip() + '*'
        if tag == 'a':
            link = safe_link(attrs.get('href'))
            return '[' + body.strip() + '](' + link + ')' if link else body
        if re.fullmatch(r'h[1-6]', tag):
            return '\n\n' + '#' * int(tag[1]) + ' ' + body.strip() + '\n\n'
        if tag == 'br':
            return '\n'
        if tag == 'hr':
            return '\n\n---\n\n'
        if tag in ('p', 'div', 'section', 'article', 'blockquote'):
            return '\n\n' + body.strip() + '\n\n'
        if tag == 'span' and 'bold' in attrs.get('style', ''):
            return '**' + body.strip() + '**'
        return body


class Resources:
    def __init__(self, graph, vault, budget):
        self.graph, self.vault, self.budget = graph, vault, budget
        self.cache, self.warnings = {}, []

    def __call__(self, url, label):
        if url in self.cache:
            return self.cache[url]
        try:
            self.graph.validate_url(url)
            if not re.fullmatch(re.escape(parse.urlsplit(self.graph.base).path) + r'resources/[^/]+/\$value', parse.urlsplit(url).path):
                raise onenote.UserError('Unsupported resource path')
            self.budget.take()
            req = request.Request(url, headers={'Authorization': 'Bearer ' + self.graph.auth.token()})
            with request.build_opener(onenote.NoRedirect()).open(req, timeout=30) as response:
                data = response.read(20 * 1024 * 1024 + 1)
                if len(data) > 20 * 1024 * 1024:
                    raise onenote.UserError('Resource exceeds 20 MiB')
                mime = response.headers.get_content_type()
            ext = Path(label).suffix.lower()
            if ext not in {'.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.docx', '.xlsx', '.pptx', '.txt', '.csv'}:
                ext = { 'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'application/pdf': '.pdf' }.get(mime, '.bin')
            name = digest(data) + ext
            path = guard(self.vault / MIRROR / ASSETS / name)
            if path.exists():
                if digest(path.read_bytes()) != digest(data):
                    raise onenote.UserError('Local attachment was edited')
            else:
                atomic(path, data)
            result = ASSETS + '/' + name
            self.cache[url] = result
            return result
        except BudgetExceeded:
            raise
        except (onenote.UserError, error.URLError, OSError, ValueError):
            self.warnings.append('Resource unavailable: ' + label[:150])
            self.cache[url] = None
            return None


def filename(page_id):
    return 'page-' + digest(page_id.encode())[:24] + '.md'


def changed_near_last_run(modified, last_started):
    # Recheck a small overlap to handle second-resolution timestamps and writes
    # that happened while the previous metadata snapshot was being fetched.
    try:
        source = datetime.fromisoformat(modified.replace('Z', '+00:00')).timestamp()
        prior = datetime.fromisoformat(last_started).timestamp()
        return source >= prior - 120
    except (ValueError, TypeError, AttributeError):
        return True


def frontmatter(meta, section, body, warnings, machine):
    link = meta.get('links', {}).get('oneNoteWebUrl', {}).get('href', '')
    title = meta.get('title') or 'Untitled'
    props = {
        'type': 'knowledge', 'layer': 'working', 'marking': 'internal',
        'purpose': ['demo', 'reference'], 'owner': 'masanori',
        'source_agent': 'claude-code', 'machine': machine, 'status': 'mirror',
        'summary': 'OneNote reference copy: ' + title, 'created': meta.get('createdDateTime') or now(),
        'aliases': [title], 'onenote_page_id': meta['id'], 'onenote_section_id': section,
        'source_url': safe_link(link), 'source_modified': meta.get('lastModifiedDateTime'),
        'synced_at': now(), 'canonical_source': 'OneNote', 'sync_managed': True,
        'conversion_warnings': sorted(set(warnings)),
    }
    yaml = '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False) for k, v in props.items())
    notice = '> OneNoteが正本です。このファイルは自動更新されます。編集はOneNoteで行ってください。\n'
    if link:
        notice += '> [元のOneNoteページを開く](' + safe_link(link) + ')\n'
    if warnings:
        notice += '> Conversion needs review: see conversion_warnings.\n'
    return ('---\n' + yaml + '\n---\n\n# ' + escape_text(title) + '\n\n' + notice + '\n' + body).encode()


def sync(graph, vault, max_pages=10000, budget=None):
    vault = guard(vault)
    budget = budget or Budget(100)
    with locked(vault) as state_dir:
        state_path = guard(state_dir / 'state.json')
        identity = {k: graph.cfg[k] for k in ('tenant', 'client_id', 'root', 'section_id')}
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'identity': identity, 'pages': {}}
        if state.get('identity') != identity or not isinstance(state.get('pages'), dict):
            raise onenote.UserError('Vault belongs to a different OneNote source or has invalid state.')
        (vault / 'AI_Outputs').mkdir(exist_ok=True, mode=0o700)
        guard(vault / MIRROR).mkdir(exist_ok=True, mode=0o700)
        report = {'started_at': now(), 'source_agent': 'claude-code', 'machine': onenote.machine_name(graph.cfg),
                  'created': [], 'updated': [], 'unchanged': 0, 'conflicts': [], 'failed': [],
                  'warnings': {}, 'not_seen': [], 'deferred': False, 'requests': 0}
        try:
            listing = graph.collection('sections/' + onenote.segment(graph.section()) +
                '/pages?$top=100&$select=id,title,lastModifiedDateTime,createdDateTime,links', limit=max_pages)
            if listing['truncated']:
                raise onenote.UserError('Page list incomplete. Increase max-pages; no mirror files changed.')
            seen = set()
            resources = Resources(graph, vault, budget)
            for meta in listing['items']:
                page_id = meta['id']
                if page_id in seen:
                    continue
                seen.add(page_id)
                path = guard(vault / MIRROR / filename(page_id))
                previous = state['pages'].get(page_id)
                if path.exists() and (not previous or digest(path.read_bytes()) != previous.get('sha256')):
                    report['conflicts'].append({'page_id': page_id, 'file': path.name, 'reason': 'local_file_changed'})
                    continue
                if previous and previous.get('format_version') == FORMAT_VERSION and path.exists() and meta.get('lastModifiedDateTime') and previous.get('modified') == meta['lastModifiedDateTime'] and previous.get('title') == meta.get('title') and not previous.get('warnings') and not changed_near_last_run(meta.get('lastModifiedDateTime'), state.get('last_started_at')):
                    report['unchanged'] += 1
                    continue
                try:
                    raw = graph.call('GET', 'pages/' + onenote.segment(page_id) + '/content')
                    raw_hash = digest(raw.encode())
                    if previous and previous.get('format_version') == FORMAT_VERSION and path.exists() and previous.get('html_sha256') == raw_hash and previous.get('title') == meta.get('title') and not previous.get('warnings'):
                        previous['modified'] = meta.get('lastModifiedDateTime')
                        report['unchanged'] += 1
                        continue
                    converter = Markdown(resources)
                    body = converter.convert(raw)
                    warnings = converter.warnings
                    output = frontmatter(meta, graph.section(), body, warnings, onenote.machine_name(graph.cfg))
                    # Check again after network/convert work to protect edits during a run.
                    if path.exists() and (not previous or digest(path.read_bytes()) != previous.get('sha256')):
                        report['conflicts'].append({'page_id': page_id, 'file': path.name, 'reason': 'edited_during_sync'})
                        continue
                    atomic(path, output)
                    state['pages'][page_id] = {'modified': meta.get('lastModifiedDateTime'),
                        'title': meta.get('title'), 'sha256': digest(output), 'html_sha256': raw_hash, 'file': path.name,
                        'warnings': sorted(set(warnings)), 'format_version': FORMAT_VERSION, 'synced_at': now()}
                    save_json(state_path, state)
                    report['updated' if previous else 'created'].append(path.name)
                    if warnings:
                        report['warnings'][path.name] = sorted(set(warnings))
                except BudgetExceeded:
                    report['deferred'] = True
                    break
                except onenote.UserError as exc:
                    report['failed'].append({'page_id': page_id, 'error': str(exc)})
                    # API outages/auth/throttle should not be hammered for every page.
                    break
            # A full metadata list is needed to distinguish absence from an API outage.
            all_ids = {p['id'] for p in listing['items']}
            report['not_seen'] = sorted(set(state['pages']) - all_ids)
        except BudgetExceeded:
            report['deferred'] = True
        except onenote.UserError as exc:
            report['failed'].append({'error': str(exc)})
        state['last_run_at'] = now()
        state['last_started_at'] = report['started_at']
        save_json(state_path, state)
        report['finished_at'], report['requests'] = now(), budget.used
        report['status'] = ('needs_attention' if report['conflicts'] or report['failed'] or report['warnings'] or report['not_seen']
                            else 'partial' if report['deferred'] else 'ok')
        save_json(state_dir / 'last-run.json', report)
        return report


def main():
    p = argparse.ArgumentParser(description='One-way OneNote -> Obsidian mirror (no writes to OneNote).')
    p.add_argument('--config', type=Path, default=onenote.DEFAULT_CONFIG)
    p.add_argument('--vault', type=Path, default=DEFAULT_VAULT)
    p.add_argument('--max-pages', type=int, default=10000)
    p.add_argument('--max-requests', type=int, default=100)
    args = p.parse_args()
    if not 1 <= args.max_requests <= 300 or not 1 <= args.max_pages <= 100000:
        raise onenote.UserError('Invalid request/page budget.')
    cfg = onenote.load_config(args.config)
    budget = Budget(args.max_requests)
    graph = onenote.Graph(cfg, transport=budget.transport)
    report = sync(graph, args.vault, args.max_pages, budget)
    onenote.emit(report)
    return 1 if report['status'] == 'needs_attention' else 0


if __name__ == '__main__':
    try:
        onenote.utf8_stdio()
        sys.exit(main())
    except (onenote.UserError, ValueError, TypeError, KeyError, OSError) as exc:
        message = str(exc) if isinstance(exc, onenote.UserError) else 'Invalid state, response, or filesystem access; no secrets printed.'
        print('Error: ' + message, file=sys.stderr)
        sys.exit(1)
