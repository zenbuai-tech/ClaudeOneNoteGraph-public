#!/usr/bin/env python3
"""Repeatable end-to-end demo; source_agent=claude-code, machine=mac-mini."""
import json
from pathlib import Path
import sys
import time
import onenote
import sync

ROOT = Path(__file__).resolve().parent
JOURNAL = ROOT / 'demo-result.json'
INITIAL = '''架空データ：企業Aの面談記録
面談日：2026-09-22
担当者の発言：来期に新設備を稼働する予定。正式な開始日は未定。
未確認事項：稼働時期と利益への影響。
source_agent: claude-code
machine: MACHINE_PLACEHOLDER
'''
UPDATE = '''【追加ヒアリング・架空】
設備の試運転は11月を予定。正式な稼働日は引き続き未定。
利益率への影響は未回答。改善すると断定しない。
'''


def run_sync(graph):
    budget = sync.Budget(100)
    reader = onenote.Graph(graph.cfg, graph.auth, budget.transport)
    report = sync.sync(reader, sync.DEFAULT_VAULT, budget=budget)
    if report['status'] != 'ok':
        raise onenote.UserError('Sync needs attention; inspect demo-vault/.onenote-sync/last-run.json')
    return report


def main():
    graph = onenote.Graph(onenote.load_config(onenote.DEFAULT_CONFIG))
    state = json.loads(JOURNAL.read_text(encoding='utf-8')) if JOURNAL.exists() else {'source_agent': 'claude-code', 'machine': onenote.machine_name(graph.cfg), 'steps': {}}
    if state.get('complete'):
        onenote.emit(state)
        return
    if 'page' not in state:
        if state.get('create_pending'):
            raise onenote.UserError('A previous creation is uncertain. Inspect OneNote before retrying; no duplicate page created.')
        state['create_pending'] = True
        sync.save_json(JOURNAL, state)
        state['page'] = graph.create('OneNote→Obsidianデモ｜企業Aの面談（架空）',
            INITIAL.replace('MACHINE_PLACEHOLDER', onenote.machine_name(graph.cfg)))
        state['create_pending'] = False
        sync.save_json(JOURNAL, state)
    page_id = state['page']['id']
    data = graph.read(page_id)
    if '正式な開始日は未定' not in data['text']:
        raise onenote.UserError('Read-back did not contain the initial source text.')
    state['steps']['direct_create_read'] = True
    state['steps']['initial_sync'] = run_sync(graph)
    sync.save_json(JOURNAL, state)
    local = sync.DEFAULT_VAULT / sync.MIRROR / sync.filename(page_id)
    if '正式な開始日は未定' not in local.read_text(encoding='utf-8'):
        raise onenote.UserError('Initial Markdown content did not match source.')
    if '設備の試運転は11月' not in data['text']:
        time.sleep(2)
        # Record before mutation: uncertain PATCH must be checked, not blindly repeated.
        if state.get('append_pending'):
            raise onenote.UserError('Previous append is uncertain. Read the source before retrying.')
        state['append_pending'] = True
        sync.save_json(JOURNAL, state)
        graph.append(page_id, UPDATE)
        state['append_pending'] = False
        sync.save_json(JOURNAL, state)
    updated = graph.read(page_id)
    if '設備の試運転は11月' not in updated['text']:
        raise onenote.UserError('Updated source not visible yet; retry later without adding another note.')
    state['steps']['direct_append_read'] = True
    state['steps']['update_sync'] = run_sync(graph)
    if '設備の試運転は11月' not in local.read_text(encoding='utf-8'):
        raise onenote.UserError('Updated Markdown not visible yet; rerun later.')
    before = local.read_bytes()
    state['steps']['repeat_sync'] = run_sync(graph)
    state['steps']['no_duplicate_no_rewrite'] = before == local.read_bytes() and not state['steps']['repeat_sync']['created'] and not state['steps']['repeat_sync']['updated']
    if not state['steps']['no_duplicate_no_rewrite']:
        raise onenote.UserError('Repeat sync changed files unexpectedly.')
    url = state['page']['links']['oneNoteWebUrl']['href']
    questions = '''---
type: knowledge
layer: working
marking: internal
purpose: [demo]
owner: masanori
source_agent: claude-code
machine: MACHINE_PLACEHOLDER
created: 2026-09-22
status: draft
summary: "架空の面談記録を使った質問案のデモ"
source_url: URL_PLACEHOLDER
---

# 企業A：次回ヒアリングの質問案（デモ）

1. **11月の試運転後、正式稼働までに何を確認しますか？**
   - 根拠：試運転は11月予定。正式稼働日は未定。
2. **正式な稼働日を決める担当者と判断時期は？**
   - 根拠：原文と追記の両方で、稼働日が未確定。
3. **利益への影響を試算できる時期と必要な情報は？**
   - 根拠：利益率への影響は未回答。改善の断定はできない。

このファイルはAIの成果物の例です。OneNote取り込みフォルダと分けて保存しています。
'''.replace('URL_PLACEHOLDER', json.dumps(url, ensure_ascii=False)).replace('MACHINE_PLACEHOLDER', onenote.machine_name(graph.cfg))
    output = sync.DEFAULT_VAULT / 'AI_Outputs' / '企業A_次回質問案_デモ.md'
    if not output.exists():
        sync.atomic(output, questions.encode())
    state['mirror_file'], state['output_file'] = str(local), str(output)
    state['complete'], state['verified_at'] = True, sync.now()
    sync.save_json(JOURNAL, state)
    onenote.emit(state)


if __name__ == '__main__':
    try:
        onenote.utf8_stdio()
        main()
    except (onenote.UserError, OSError, ValueError, KeyError) as exc:
        print(str(exc) if isinstance(exc, onenote.UserError) else 'Demo failed; inspect journal without exposing authentication data.', file=sys.stderr)
        sys.exit(1)
