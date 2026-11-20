"""Run a matched Dirac comparison, save SQLite records, and render a static HTML dashboard."""
import argparse
from collections import Counter
from datetime import datetime, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'gpt-6-luna'
# USD per million tokens: Dirac OpenAI Native Luna <=272k reference tier.
# This is a counterfactual estimate, NOT Codex subscription billing.
RATES = (.10, .50, .01, .125)
CASES = {
    'Single worker': ('^1 !1', 'Ask one worker to return exactly ALPHA.', ('ALPHA',), ('ALPHA',)),
    'Parallel workers': ('^2 !2', 'In a single parallel batch, ask one worker for exactly BLUE and another for exactly SEVEN.', ('BLUE', 'SEVEN'), ('BLUE', 'SEVEN')),
    'Dependent workers': ('^2 !1', 'Ask a worker for the uppercase word for the animal that barks. Then ask a second worker to reverse the first worker’s answer and return only the reversed word.', ('DOG', 'GOD'), ('GOD',)),
}
ADVANCED = {
    'Coding · SQLite usage ledger': ('^3 !2',
        'Implement a single SQLite SELECT query for table usage(event_id TEXT, revision INTEGER, team TEXT, tokens INTEGER). '
        'For each event keep only the highest revision (event_id and revision are unique together). '
        'Discard superseded rows even if the latest revision moved teams. Return team, total_tokens and events per team, '
        'with NULL tokens counted as zero, sorted by total_tokens DESC then team ASC. Ask workers to design and independently review the query with a stronger model if worker model selection is available. '
        'Return only executable SQL, with no Markdown.', (), ('SQL passes frozen fixtures',)),
    'Research · Vendor evidence review': ('^3 !2',
        'Delegate a source-grounded procurement analysis. Select the cheapest eligible vendor at 120,000 monthly units. '
        'Eligibility: p95 latency at most 150 ms, data retention at most 7 days, and monthly cost at most $135. '
        'Use only this supplied source packet. [A] Atlas signed price sheet: $90 for 100,000 units plus $2 per started block of 1,000. '
        '[B] Atlas audit: 142 ms p95; retention 7 days. [C] Birch signed offer: $125 flat and 130 ms p95. '
        '[D] Birch binding DPA: default retention 30 days; a signed 7-day addendum costs $15/month. '
        '[E] Cedar website: $105 and 120 ms average. [F] Cedar independent benchmark: 170 ms p95; retention 5 days. '
        'Check both eligibility and arithmetic. Return JSON only, with keys winner, monthly_usd, atlas_sources (array of supporting source IDs), '
        'birch_adjusted_usd, and cedar_p95_ms.', (), ('Atlas · $130 · A/B · Birch $140 · Cedar 170 ms',)),
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS comparisons (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, rates_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY, comparison_id TEXT NOT NULL REFERENCES comparisons(id),
 scenario TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('stock','fork')),
 model TEXT NOT NULL, provider TEXT NOT NULL, effort TEXT NOT NULL, brief TEXT NOT NULL,
 expected TEXT NOT NULL, answer TEXT, correct INTEGER NOT NULL, exact_workers INTEGER NOT NULL,
 exit_code INTEGER, elapsed REAL NOT NULL, ledger TEXT,
 input INT, output INT, cache_read INT, cache_write INT, reported_cost REAL, estimated_cost REAL,
 UNIQUE(comparison_id, scenario, mode)
);
CREATE TABLE IF NOT EXISTS workers (
 run_id INTEGER NOT NULL REFERENCES runs(id), card_id TEXT NOT NULL,
 name TEXT NOT NULL, task TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,
 result TEXT, start_ms INT, end_ms INT, model TEXT NOT NULL,
 input INT, output INT, cache_read INT, cache_write INT, reported_cost REAL, estimated_cost REAL,
 PRIMARY KEY(run_id, card_id)
);
'''


def usage(record, keys):
    return tuple(record.get(k) for k in keys)


def aggregate(samples):
    if not samples:
        return (None,) * 5
    return tuple(sum(column) if None not in column else None for column in zip(*samples))


def estimate(values, model=MODEL):
    if model != MODEL:
        return None  # No verified reference tariff for Terra or GPT-6.2 Sol.
    return sum(n * rate for n, rate in zip(values[:4], RATES)) / 1_000_000 if None not in values[:4] else None


def parse_events(stdout):
    requests, cards = {}, {}
    answer = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line).get('content', {})
        except (ValueError, AttributeError):
            continue
        if not isinstance(event, dict):
            continue
        status = event.get('status')
        if isinstance(status, dict) and 'id' in status and 'tokensIn' in status:
            requests[status['id']] = status
        card = event.get('card') or {}
        if card.get('kind') == 'task_completion' and card.get('status') == 'success':
            answer = card.get('body')
        source = card.get('rawInput') or {}
        if not isinstance(source, dict) or not source.get('isSubagent'):
            continue
        cards[card['id']] = card
    parent = aggregate([usage(s, ('tokensIn', 'tokensOut', 'cacheReads', 'cacheWrites', 'cost')) for s in requests.values()])
    workers = []
    for card in sorted(cards.values(), key=lambda c: (c.get('startTime') or 0, c['id'])):
        source = card['rawInput']
        result = card.get('rawOutput') or {}
        trajectory = [e['text'] for e in result.get('trajectory', []) if e.get('type') == 'result']
        body = card.get('body') or ''
        if not trajectory:
            trajectory = re.findall(r'^- ✓ (.+)$', body, re.M)
        values = usage(result.get('usage') or {}, ('inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens', 'totalCost'))
        workers.append((card['id'], source['agentName'], source['taskTitle'], source['prompt'], result.get('status', card['status']),
                        trajectory[-1] if trajectory else None, card.get('startTime'), card.get('endTime'), source.get('workerModel') or MODEL, *values,
                        estimate(values, source.get('workerModel') or MODEL), source.get('workerEffort') or 'low'))
    return answer, parent, workers


def verify_coding(answer):
    query = (answer or '').strip()
    if not re.match(r'^(select|with)\b', query, re.I):
        return False
    fixtures = [
        ([('a', 1, 'A', 99), ('a', 2, 'B', 7), ('b', 1, 'A', None), ('c', 1, 'A', 7)], [('A', 7, 2), ('B', 7, 1)]),
        ([('x', 2, 'old', 22), ('x', 9, 'new', None), ('y', 10, 'old', 10), ('y', 1, 'new', 1), ('z', 1, 'new', 10)], [('new', 10, 2), ('old', 10, 1)]),
        ([], []),
    ]
    for rows, expected in fixtures:
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE usage(event_id TEXT, revision INTEGER, team TEXT, tokens INTEGER)')
            db.executemany('INSERT INTO usage VALUES (?,?,?,?)', rows)
            db.commit()
            allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
            db.set_authorizer(lambda action, first, second, *_: sqlite3.SQLITE_OK if action in allowed and not
                              (action == sqlite3.SQLITE_FUNCTION and second in ('load_extension', 'readfile', 'writefile')) else sqlite3.SQLITE_DENY)
            db.set_progress_handler(lambda: 1, 30_000)
            try:
                if db.execute(query).fetchmany(len(expected) + 1) != expected:
                    return False
            except sqlite3.Error:
                return False
    return True


def verify_research(answer):
    try:
        result = json.loads(answer or '')
        return (result['winner'] == 'Atlas' and result['monthly_usd'] == 130 and
                sorted(result['atlas_sources']) == ['A', 'B'] and result['birch_adjusted_usd'] == 140 and result['cedar_p95_ms'] == 170)
    except (ValueError, TypeError, KeyError):
        return False


def run(db, comparison, scenario, mode, options):
    controls, task, expected_workers, expected_answer = (CASES | ADVANCED)[scenario]
    brief = f'{controls} {task} Use only delegation and response tools.'
    if scenario in CASES:
        brief += ' Give the final word(s) only, separated by a space if there are two.'
    if scenario in ADVANCED and mode == 'fork':
        brief += ' Select your worker models and effort based on task complexity; consider a stronger worker for independent review.'
    with tempfile.TemporaryDirectory(prefix='dirac-compare-') as workspace:
        common = ['--config', str(options.config), '--cwd', workspace, '--json', '--timeout', '45',
                  '--provider', 'openai-codex', '--model', MODEL, '--reasoning-effort', 'low']
        command = ([options.node, str(ROOT / 'cli/dist/cli.mjs'), 'orchestrate', '--approve'] if mode == 'fork' else
                   [options.stock, 'task', '--act', '--subagents', '--no-index'])
        start = time.monotonic()
        try:
            process = subprocess.run(command + common + [brief], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=55)
            stdout, stderr, code = process.stdout, process.stderr, process.returncode
        except subprocess.TimeoutExpired as failure:
            stdout = (failure.stdout or b'').decode(errors='replace')
            stderr = (failure.stderr or b'').decode(errors='replace')
            code = 124
        elapsed = time.monotonic() - start
    answer, parent, workers = parse_events(stdout)
    judge = verify_coding if scenario.startswith('Coding') else verify_research if scenario.startswith('Research') else (lambda value: tuple((value or '').split()) == expected_answer)
    correct = code == 0 and judge(answer)
    remaining_expected = Counter(expected_workers)
    worker_exact = 0
    for worker in workers:
        if remaining_expected[worker[5]] > 0:
            worker_exact += 1
            remaining_expected[worker[5]] -= 1
    ledger = next((line for line in stderr.splitlines() if line.startswith('Orchestrator ledger:')), None)
    with db:
        cur = db.execute('''INSERT INTO runs (comparison_id,scenario,mode,model,provider,effort,brief,expected,answer,correct,
                         exact_workers,exit_code,elapsed,ledger,input,output,cache_read,cache_write,reported_cost,estimated_cost)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (comparison, scenario, mode, MODEL, 'openai-codex', 'low', brief, ' '.join(expected_answer), answer,
                          correct, worker_exact, code, elapsed, ledger, *parent, estimate(parent)))
        db.execute('UPDATE runs SET requested=? WHERE id=?', (int(controls.split()[0][1:]), cur.lastrowid))
        db.executemany('INSERT INTO workers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', [(cur.lastrowid, *worker) for worker in workers])
    print(f'{scenario} / {mode}: {elapsed:.2f}s, exit {code}, {len(workers)} worker card(s)', flush=True)


def text(value):
    return escape(str(value)) if value is not None else '—'


def metrics(values):
    return ''.join(f'<div><small>{label}</small><b>{text(values[key])}</b></div>' for key, label in
                   [('input', 'Input'), ('output', 'Output'), ('cache_read', 'Cache read'), ('cache_write', 'Cache write')]) + ''.join(
        f'<div><small>{label}</small><b>{"—" if values[key] is None else f"${values[key]:.6f}"}</b></div>' for key, label in
        [('reported_cost', 'Dirac reported'), ('estimated_cost', 'API reference estimate')])


def render_run(row, workers):
    keys = ('input', 'output', 'cache_read', 'cache_write', 'reported_cost', 'estimated_cost')
    totals = dict(zip(keys, aggregate([tuple(item[key] for key in keys) for item in [row, *workers]]), strict=True))
    heading = 'Normal Dirac' if row['mode'] == 'stock' else 'Orchestrator fork'
    start = min((w['start_ms'] for w in workers if w['start_ms'] is not None), default=None)
    end = max((w['end_ms'] for w in workers if w['end_ms'] is not None), default=None)
    branches = []
    for w in workers:
        timeline = ''
        if start is not None and end is not None and end > start and w['start_ms'] is not None and w['end_ms'] is not None:
            offset = 100 * (w['start_ms'] - start) / (end - start)
            width = 100 * (w['end_ms'] - w['start_ms']) / (end - start)
            timeline = f'<div class="timeline" title="Relative to first worker start"><span style="margin-left:{offset:.2f}%;width:{width:.2f}%"></span></div>'
        branches.append(f'<li><strong>{text(w["name"])} · {text(w["task"])}</strong><small>{text(w["model"])} / {text(w["worker_effort"])} · {text(w["status"])}</small>'
                        f'{timeline}<p>{text(w["prompt"])}</p><p><b>Result:</b> {text(w["result"])}</p><div class="metrics">{metrics(w)}</div></li>')
    expected_workers = len((CASES | ADVANCED)[row['scenario']][2])
    worker_grade = f'{row["exact_workers"]}/{expected_workers} expected worker replies exact' if expected_workers else f'{len(workers)} observed workers'
    return (f'<article class="{row["mode"]}"><h3>{heading}</h3><p>{text(row["provider"])} / {text(row["model"])} / {text(row["effort"])}</p>'
            f'<p><b>{row["elapsed"]:.2f} s</b> · final {"correct" if row["correct"] else "not exact"} · '
            f'{worker_grade} · exit {text(row["exit_code"])}</p>'
            f'<p><b>Final:</b> {text(row["answer"])} <small>Expected: {text(row["expected"])}</small></p>'
            f'<details><summary>Task brief and parent usage</summary><p>{text(row["brief"])}</p><div class="metrics">{metrics(row)}</div></details>'
            f'<p>{text(row["ledger"]) if row["ledger"] else "No enforced ledger"}</p><h4>Total · parent + workers</h4>'
            f'<div class="metrics">{metrics(totals)}</div><h4>Worker branches</h4><ul class="tree">{"".join(branches) or "<li>No worker cards captured</li>"}</ul></article>')


CSS = '''
:root{color-scheme:light;font:15px/1.45 system-ui;background:#eef2f6;color:#16243b}body{max-width:1280px;margin:0 auto;padding:20px}h1,h2,h3,h4,p{margin:0 0 10px}header{border-bottom:1px solid #ccd8e3;margin-bottom:18px}section{margin:18px 0}.pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}article{background:white;border:1px solid #ccd8e3;border-top:4px solid #586d91;border-radius:10px;padding:14px;overflow-wrap:anywhere}article.fork{border-top-color:#078273}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px;margin-bottom:10px}.metrics>div{background:#eef3f7;border-radius:5px;padding:5px}.metrics b,small{display:block}small{color:#455976}summary{cursor:pointer}.tree{list-style:none;border-left:2px solid #9cabc1;padding:0 0 0 13px}.tree li{position:relative;margin-bottom:12px;padding:7px;background:#f7f9fb;border-radius:7px}.tree li:before{content:"";position:absolute;left:-13px;top:16px;width:12px;border-top:2px solid #9cabc1}.timeline{background:#e0e8ef;height:6px;margin:6px 0}.timeline span{display:block;background:#087969;height:100%}@media(max-width:620px){.pair{grid-template-columns:1fr}}
'''


def migrate(db):
    for table, column, kind in [('runs', 'requested', 'INTEGER'), ('workers', 'worker_effort', 'TEXT')]:
        if column not in {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
    for scenario, (controls, *_) in (CASES | ADVANCED).items():
        db.execute('UPDATE runs SET requested=? WHERE requested IS NULL AND scenario=?', (int(controls.split()[0][1:]), scenario))
    db.execute("UPDATE workers SET worker_effort='low' WHERE worker_effort IS NULL")
    db.commit()


def combined(db):
    runs = db.execute('SELECT * FROM runs').fetchall()
    workers = db.execute('SELECT workers.*, runs.mode FROM workers JOIN runs ON runs.id=workers.run_id').fetchall()
    pairs = {}
    for row in runs:
        pairs.setdefault((row['comparison_id'], row['scenario']), {})[row['mode']] = row['correct']
    blocks = []
    for mode, name in [('stock', 'Normal Dirac'), ('fork', 'Orchestrator fork')]:
        other = 'fork' if mode == 'stock' else 'stock'
        scores = [p[mode] - p[other] for p in pairs.values() if len(p) == 2]
        rs = [row for row in runs if row['mode'] == mode]
        ws = [row for row in workers if row['mode'] == mode]
        keys = ('input', 'output', 'cache_read', 'cache_write', 'reported_cost', 'estimated_cost')
        vals = dict(zip(keys, aggregate([tuple(row[k] for k in keys) for row in rs + ws]), strict=True)) if rs else dict.fromkeys(keys)
        cost_rows = [row['estimated_cost'] for row in rs + ws if row['estimated_cost'] is not None]
        subtotal = f'<p>Priced subtotal: ${sum(cost_rows):.6f} ({len(cost_rows)}/{len(rs)+len(ws)} parent/worker records); full estimate unavailable.</p>' if cost_rows and vals['estimated_cost'] is None else ''
        requested = sum(row['requested'] or 0 for row in rs)
        blocks.append(f'<article class="{mode}"><h3>{name}</h3><p><strong>{scores.count(1)} wins · {scores.count(-1)} losses · {scores.count(0)} ties</strong></p>'
                      f'<p>{sum(r["correct"] for r in rs)}/{len(rs)} correct · {sum(r["elapsed"] for r in rs):.2f} s · {len(ws)}/{requested} workers observed / requested cap</p>'
                      f'<p>Actual billed spend: unavailable</p><div class="metrics">{metrics(vals)}</div>{subtotal}</article>')
    return '<h2>Combined results</h2><p>A win means only that mode passed the task judge. Both pass or both fail = tie. Worker count is observed cards; requested is the sum of task launch caps.</p><div class="pair">' + ''.join(blocks) + '</div>'


def render(db, output):
    comparisons = db.execute('SELECT * FROM comparisons ORDER BY created_at DESC').fetchall()
    sections = []
    for comparison in comparisons:
        sections.append(f'<h2>{text(comparison["created_at"])}</h2>')
        for scenario in CASES | ADVANCED:
            rows = db.execute('SELECT * FROM runs WHERE comparison_id=? AND scenario=? ORDER BY CASE mode WHEN \'stock\' THEN 0 ELSE 1 END',
                              (comparison['id'], scenario)).fetchall()
            if not rows:
                continue
            cards = [render_run(r, db.execute('SELECT * FROM workers WHERE run_id=? ORDER BY start_ms, card_id', (r['id'],)).fetchall()) for r in rows]
            seconds_max = max((r['elapsed'] for r in rows), default=1) or 1
            bars = ''.join(f'<div><small>{"Normal" if r["mode"] == "stock" else "Fork"} · {r["elapsed"]:.2f} s</small>'
                           f'<div class="timeline"><span style="width:{100*r["elapsed"]/seconds_max:.2f}%"></span></div></div>' for r in rows)
            sections.append(f'<section><h3>{text(scenario)}</h3><div class="pair" aria-label="Elapsed time comparison">{bars}</div><div class="pair">{"".join(cards)}</div></section>')
    html = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Dirac orchestration benchmark</title><style>{CSS}</style><body><header><h1>Dirac orchestration benchmark</h1>'
            '<p>Normal Dirac on the left; orchestrator fork on the right. Worker bars show relative start and end times, not dependencies.</p>'
            '<p>Exact replies count punctuation. Dirac’s reported $0 on Codex does not establish subscription cost. Estimates use the '
            'OpenAI Native Luna standard ≤272k reference rates: $0.10 input, $0.50 output, $0.01 cache read, $0.125 cache write per million. '
            'Estimates are not billed amounts. No reference tariff is available for Terra or GPT-6.2 Sol. — means unavailable.</p></header>' + combined(db) + ''.join(sections) + '</body></html>')
    output.write_text(html, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='Authenticated Codex config; required for live runs')
    parser.add_argument('--stock', default='dirac', help='Normal Dirac executable')
    parser.add_argument('--node', default='node', help='Node executable for the fork')
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--html', type=Path, required=True)
    parser.add_argument('--suite', choices=('basic', 'advanced', 'all'), default='all')
    parser.add_argument('--render-only', action='store_true')
    options = parser.parse_args()
    if not options.render_only and not options.config:
        parser.error('--config is required for a live comparison')
    if options.db.resolve() == options.html.resolve():
        parser.error('--db and --html must be different files')
    with sqlite3.connect(options.db) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript(SCHEMA)
        migrate(db)
        if not options.render_only:
            comparison = str(uuid4())
            with db:
                db.execute('INSERT INTO comparisons VALUES (?,?,?)', (comparison, datetime.now(timezone.utc).isoformat(timespec='seconds'), json.dumps(RATES)))
            for index, scenario in enumerate({'basic': CASES, 'advanced': ADVANCED, 'all': CASES | ADVANCED}[options.suite]):
                for mode in (('stock', 'fork') if index % 2 == 0 else ('fork', 'stock')):
                    run(db, comparison, scenario, mode, options)
                render(db, options.html)
        render(db, options.html)


if __name__ == '__main__':
    main()
