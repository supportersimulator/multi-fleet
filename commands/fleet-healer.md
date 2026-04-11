---
description: "Spawn background healing agents to restore broken fleet channels"
disable-model-invocation: true
---

Invoke the multi-fleet:fleet-healer skill and follow it exactly as presented to you. Then run: `curl -s http://127.0.0.1:8855/channels | python3 -c "import sys,json;d=json.load(sys.stdin);broken=[f'{n}: {c}' for n,cs in d.items() for c,s in cs.items() if s!='ok'];print('All channels healthy' if not broken else 'Broken: '+', '.join(broken))"` and display the results.
