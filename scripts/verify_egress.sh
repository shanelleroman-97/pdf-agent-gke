#!/bin/bash
# Prove the sandbox network lockdown from inside a warm worker pod:
# api.anthropic.com must be reachable; everything else must time out.
set -uo pipefail
NS=${NAMESPACE:-pdf-agent}
POD=$(kubectl -n "$NS" get pods -l app=pdf-agent-worker --no-headers | awk '$2=="1/1"{print $1; exit}')
[ -n "$POD" ] || { echo "no ready worker pod in $NS"; exit 1; }
echo "testing from $POD"
fail=0
check() {  # name url expect(allow|deny)
  r=$(kubectl -n "$NS" exec "$POD" -- python3 -c "
import urllib.request, urllib.error
try:
    urllib.request.urlopen('$2', timeout=6); print('allow')
except urllib.error.HTTPError: print('allow')
except Exception: print('deny')" 2>/dev/null | tail -1)
  if [ "$r" = "$3" ]; then s=ok; else s=FAIL; fail=1; fi
  printf '  %-4s %-24s %s (expected %s)\n' "$s" "$1" "$r" "$3"
}
check api.anthropic.com      https://api.anthropic.com/                 allow
check example.com            https://example.com/                       deny
check storage.googleapis.com https://storage.googleapis.com/            deny
check metadata-server        http://169.254.169.254/computeMetadata/v1/ deny
check kubernetes-api         https://kubernetes.default.svc/            deny
check 8.8.8.8-by-ip          https://8.8.8.8/                           deny
exit $fail
