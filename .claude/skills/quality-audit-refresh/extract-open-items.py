#!/usr/bin/env python3
"""Pull every still-open item out of the audit doc into open-items.json for re-verification.

  extract-open-items.py docs/audit/<doc>.md <out-dir>

Reads the P1 list, the P2 list, every "<Tn> findings" section, every "New debt"
paragraph, and the completeness scoreboard's missing-pieces cells — every item
these sections carry, regardless of Followup-ledger status. It does not read the
ledger itself, so an item already closed there still gets re-extracted and
re-verified here; that re-verify pass is what confirms it's actually fixed, at
the cost of spending a verify call on something the ledger already knew. If the
doc's own sections are kept current with the ledger on each refresh, this is
find-nothing-new rather than wasted work — depends on the doc's own discipline,
not this script.

Parses by exact heading/row text, not a defined grammar: an equally reasonable
audit doc using different heading levels or table columns silently yields zero
items rather than an error. If the printed count looks wrong, check the doc's
headings against this script's patterns before trusting `openCount`.
"""
import re, json, sys, os
from collections import Counter
if len(sys.argv) != 3:
    sys.exit('usage: extract-open-items.py docs/audit/<doc>.md <out-dir>')
with open(sys.argv[1], encoding='utf-8') as f:
    doc = f.read()
out = sys.argv[2]
os.makedirs(out, exist_ok=True)
items = []
def section(start, end):
    i = doc.find(start)
    if i < 0: return ''
    j = doc.find(end, i + len(start)); return doc[i:j if j > 0 else None]
for m in re.finditer(r'^\d+\. \*\*(.+?)\*\*(.*)$', section('### P1', '### P2'), re.M):
    items.append({'kind': 'P1', 'title': m.group(1), 'detail': m.group(2).strip()[:600]})
for m in re.finditer(r'^- \*\*(.+?)\*\*(.*)$', section('### P2', '\n## '), re.M):
    items.append({'kind': 'P2', 'title': m.group(1), 'detail': m.group(2).strip()[:600]})
for hdr in re.finditer(r'^## (T\d+) findings', doc, re.M):
    sec = section(hdr.group(0), '\n## ')
    for m in re.finditer(r'^- \*\*(.+?)\*\*(.*)$', sec, re.M):
        items.append({'kind': hdr.group(1) + '-finding', 'title': m.group(1), 'detail': m.group(2).strip()[:600]})
    nd = re.search(r'\*\*New debt .*?\*\*.*?:(.*?)\n\n', sec, re.S)
    if nd:
        for part in re.split(r';\s+(?=[a-z`])', nd.group(1).strip()):
            items.append({'kind': hdr.group(1) + '-newdebt', 'title': part.strip()[:160], 'detail': part.strip()[:500]})
for m in re.finditer(r'^\| (.+?) \| (T\d+) \| (\d+)% \| (.+?) \|$', section('## Feature completeness scoreboard', '\n## '), re.M):
    feat, tr, pct, missing = m.groups()
    if int(pct) >= 100:
        continue  # complete; the missing-pieces cell is a conventional empty marker, not a gap
    for piece in re.split(r';\s+', missing):
        piece = piece.strip()
        if not piece or piece in ('—', '-', 'none', 'n/a'):
            continue
        items.append({'kind': 'completeness-gap', 'feature': feat, 'tranche': tr, 'pct': int(pct), 'title': piece})
for i, it in enumerate(items): it['id'] = i
with open(out + '/open-items.json', 'w', encoding='utf-8') as f:
    json.dump(items, f, indent=1)
print(dict(Counter(i['kind'] for i in items)), 'total', len(items))
