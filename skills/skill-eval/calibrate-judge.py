#!/usr/bin/env python3
"""Measure the skill-eval judge against hand labels, then correct pass rates for its errors.

  calibrate-judge.py sample [--n 20]  -> results/judge-labels.md (blind) + results/judge-labels.key.json
  calibrate-judge.py score            -> TPR/TNR per split, writes results/judge-calibration.json
  calibrate-judge.py correct W O      -> Rogan-Gladen corrected with/without pass rates (called by run-eval.sh)

TPR = judge says PASS when the human says PASS. TNR = judge says FAIL when the human says FAIL.
Raw agreement hides a judge that always says PASS on a mostly-PASS set, so it is not reported.
Split: dev rows are the only ones you may look at while rewriting the judge prompt. Test rows
are scored once. Until the judge prompt has been tuned, both splits measure the same judge
and the calibration uses all rows.
"""
import json, random, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = HERE / 'results'
LABELS, KEY, CAL = RES / 'judge-labels.md', RES / 'judge-labels.key.json', RES / 'judge-calibration.json'


def suites():
    out = {}
    for f in (HERE / 'suites').glob('*.json'):
        s = json.loads(f.read_text())
        for sc in s['scenarios']:
            out[(s['skill'], sc['id'])] = sc
    return out


def judge_model(run):
    m = re.search(r'judge: `([^`]+)`', (run / 'report.md').read_text()) if (run / 'report.md').exists() else None
    return m.group(1) if m else None


def sample(n):
    S, rows = suites(), []
    for run in sorted(p for p in RES.iterdir() if (p / 'raw.jsonl').exists()):
        for line in (run / 'raw.jsonl').read_text().splitlines():
            d = json.loads(line)
            if d['verdict'] not in ('PASS', 'FAIL'):
                continue
            sc = next((v for (skill, sid), v in S.items() if sid == d['sid'] and run.name.startswith(skill + '-')), None)
            if sc:
                rows.append({**d, 'result_dir': run.name, 'judge_model': judge_model(run), 'prompt': sc['prompt'], 'rubric': sc['rubric']})
    # TPR/TNR belong to one judge model; calibrate the one that graded the most rows.
    model = max({r['judge_model'] for r in rows}, key=lambda m: sum(r['judge_model'] == m for r in rows))
    rows = [r for r in rows if r['judge_model'] == model]
    rng = random.Random(42)
    picked = []
    for verdict in ('PASS', 'FAIL'):
        pool = [r for r in rows if r['verdict'] == verdict]
        rng.shuffle(pool)
        runs_seen, chosen = set(), []
        for r in pool:  # one row per run first, so the set spans skills, then fill
            if r['result_dir'] not in runs_seen and len(chosen) < n:
                chosen.append(r); runs_seen.add(r['result_dir'])
        chosen += [r for r in pool if r not in chosen][: n - len(chosen)]
        for i, r in enumerate(chosen):
            r['split'] = 'dev' if i % 2 == 0 else 'test'
        picked += chosen
    rng.shuffle(picked)
    key, md = {}, [
        '# skill-eval judge labels',
        '',
        'Read the rubric and the response, then write PASS or FAIL after `LABEL:`. Apply the rubric only.',
        'The judge verdict is hidden in `judge-labels.key.json`; do not open it until every row is labeled.',
        'Responses over 4000 characters were stored truncated. If the truncation decides the row, write SKIP.',
        'Mark the first thing wrong and stop; later problems usually follow from it.',
        'NOTE is optional free text. Use it when the rubric itself is wrong: it passes a response you would',
        'reject, or misses the failure that matters. `score` lists every note, and those are rubric fixes,',
        'which the label alone cannot carry (Husain/Shankar call this criteria drift).',
        '',
    ]
    for i, r in enumerate(picked, 1):
        rid = f'L{i:02d}'
        key[rid] = {k: r[k] for k in ('result_dir', 'sid', 'arm', 'run', 'verdict', 'split', 'judge_model')}
        md += [f'## {rid}', '', '**Task:**', '', '```', r['prompt'][:1500], '```', '',
               '**Rubric:**', '', '```', r['rubric'], '```', '', '**Response:**', '', '```', r['response'], '```', '',
               'LABEL: ', 'NOTE: ', '']
    LABELS.write_text('\n'.join(md))
    KEY.write_text(json.dumps(key, indent=1))
    print(f'{len(picked)} rows judged by {model} ({n} PASS, {n} FAIL) -> {LABELS}')


def rates(pairs):
    tp = sum(h == 'PASS' and j == 'PASS' for h, j in pairs)
    tn = sum(h == 'FAIL' and j == 'FAIL' for h, j in pairs)
    p = sum(h == 'PASS' for h, _ in pairs)
    f = sum(h == 'FAIL' for h, _ in pairs)
    return (tp / p if p else None), (tn / f if f else None), p, f


def score():
    key = json.loads(KEY.read_text())
    labels = dict(re.findall(r'^## (L\d+)\n.*?^LABEL:[ \t]*(\w*)', LABELS.read_text(), re.M | re.S))
    missing = [k for k in key if labels.get(k, '').upper() not in ('PASS', 'FAIL', 'SKIP')]
    if missing:
        sys.exit(f'{len(missing)} rows unlabeled: {", ".join(missing[:10])}')
    notes = [(k, n.strip()) for k, n in re.findall(r'^## (L\d+)\n.*?^NOTE:[ \t]*([^\n]*)$', LABELS.read_text(), re.M | re.S) if n.strip()]
    for k, n in notes:
        print(f'note {k} [{key[k]["result_dir"]} / {key[k]["sid"]}]: {n}')
    out = {'judge_model': next(iter(key.values()))['judge_model']}
    for split in ('dev', 'test', 'all'):
        pairs = [(labels[k].upper(), v['verdict']) for k, v in key.items()
                 if labels[k].upper() != 'SKIP' and split in ('all', v['split'])]
        tpr, tnr, p, f = rates(pairs)
        out[split] = {'tpr': tpr, 'tnr': tnr, 'human_pass': p, 'human_fail': f}
        fmt = lambda x: 'n/a' if x is None else f'{100 * x:.0f}%'
        print(f'{split:5} TPR {fmt(tpr)} (n={p})  TNR {fmt(tnr)} (n={f})')
    a = out['all']
    if None in (a['tpr'], a['tnr']) or a['tpr'] + a['tnr'] - 1 <= 0.1:
        sys.exit('judge no better than chance on these labels; fix the judge before trusting any verdict')
    if min(a['tpr'], a['tnr']) < 0.8:
        print('below the 80% floor: rewrite the judge prompt against dev rows only, then re-score test once')
    CAL.write_text(json.dumps(out, indent=1))
    print(f'-> {CAL}')


def correct(w, o):
    c = json.loads(CAL.read_text())['all']
    tpr, tnr = c['tpr'], c['tnr']
    rg = lambda pct: min(1.0, max(0.0, (pct / 100 + tnr - 1) / (tpr + tnr - 1))) * 100
    cw, co = rg(w), rg(o)
    print(f'_Judge calibration: TPR {100 * tpr:.0f}% (n={c["human_pass"]}), TNR {100 * tnr:.0f}% (n={c["human_fail"]}). '
          f'Rogan-Gladen corrected: with {cw:.0f}% vs without {co:.0f}% -> {cw - co:+.0f} pts._\n')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'sample':
        sample(int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[2] == '--n' else 20)
    elif cmd == 'score':
        score()
    elif cmd == 'correct':
        correct(float(sys.argv[2]), float(sys.argv[3]))
    else:
        sys.exit(__doc__)
