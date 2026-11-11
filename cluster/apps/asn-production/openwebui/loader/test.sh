#!/usr/bin/env bash
# Tests for loader.py against a real Apache Tika. Requires docker. Leaves nothing behind.
#
# Asserts on the extracted text (the artifact), not on HTTP status alone: the email headers
# Open WebUI's own Tika loader drops must be present, a forwarded email's inner headers and
# an .xls attachment's cells must appear, and an .eml sent as application/octet-stream (as
# Windows browsers often send it) must extract the same as one sent as message/rfc822.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
TIKA_IMAGE="${TIKA_IMAGE:-apache/tika:3.2.3.0-full}"
PY_IMAGE="${PY_IMAGE:-python:3.13-slim}"
run="eml-loader-test-$$"
net="$run-net"
key="$(openssl rand -hex 24)"   # throwaway, lives only in this test's containers

# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  docker rm -f "$run-tika" "$run-loader" >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$net" >/dev/null
docker run -d --name "$run-tika" --network "$net" "$TIKA_IMAGE" >/dev/null
docker run -d --name "$run-loader" --network "$net" \
  -e TIKA_URL="http://$run-tika:9998" -e LOADER_API_KEY="$key" \
  -v "$here/loader.py:/loader.py:ro" "$PY_IMAGE" python /loader.py >/dev/null

rc=0
if docker run -i --rm --network "$net" -e KEY="$key" -e LOADER="http://$run-loader:8000" \
  -v "$here/fixtures:/fixtures:ro" "$PY_IMAGE" python - <<'EOF'
import json, os, sys, time, urllib.error, urllib.request
from email.message import EmailMessage

LOADER, KEY = os.environ["LOADER"], os.environ["KEY"]
xls = open("/fixtures/budget.xls", "rb").read()

def email(frm, to, subject, date, body, cc=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = frm, to, subject, date
    if cc:
        m["Cc"] = cc
    m.set_content(body)
    return m

inner = email("Sam Client <sam@example.com>", "alice@example.com", "Q3 budget follow-up",
              "Mon, 3 Nov 2026 09:12:00 +1100",
              "Hi Alice,\nThe Q3 travel line is over by $4,210. Can you confirm the revised figure is $18,950?\nThanks, Sam")
# Tika reads the HTML alternative when there is one, so it carries the same sentence, on one
# line long enough that quoted-printable soft-wraps it (the default loader split "$=\n18,950").
inner.add_alternative("<p>Hi Alice,</p><p>The Q3 travel line is over by <b>$4,210</b>. Can you "
                      "confirm the revised figure is <b>$18,950</b>?</p><p>Thanks, Sam</p>", subtype="html")
inner_bytes = bytes(inner)
fwd = email("Alice <alice@example.com>", "boss@example.com", "Fwd: Q3 budget follow-up",
            "Tue, 4 Nov 2026 08:30:00 +1100", "FYI, see below. Numbers attached.", cc="finance@example.com")
fwd.add_attachment(inner_bytes, maintype="message", subtype="rfc822", filename="Q3 budget follow-up.eml")
fwd.add_attachment(xls, maintype="application", subtype="vnd.ms-excel", filename="budget.xls")

def put(data, ctype, name, key=KEY):
    req = urllib.request.Request(f"{LOADER}/process", data=data, method="PUT", headers={
        "Content-Type": ctype, "X-Filename": name, "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None

for _ in range(60):  # Tika takes a few seconds to start
    try:
        urllib.request.urlopen(f"{LOADER}/health", timeout=2)
        if put(b"x", "text/plain", "probe.txt")[0] == 200:
            break
    except Exception:
        pass
    time.sleep(2)

failures = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)

# Probe for the log check after this script: an unauthenticated request whose target carries an
# email address and a terminal escape. It must reach the loader (404), or the log check below
# would pass on a probe that never arrived.
try:
    urllib.request.urlopen(LOADER + "/probe-LEAK@example.com?q=%1b[31mLEAK", timeout=5)
    probe = 200
except urllib.error.HTTPError as e:
    probe = e.code
except Exception:
    probe = None
check("log probe reached the loader (404)", probe == 404)

code, doc = put(inner_bytes, "message/rfc822", "mail.eml")
text = doc["page_content"] if doc else ""
check("eml: 200", code == 200)
check("eml: From header kept", "From: Sam Client <sam@example.com>" in text)
check("eml: To header kept", "To: alice@example.com" in text)
check("eml: Date kept (as UTC)", "Date (UTC): 2026-11-02T22:12:00Z" in text)
check("eml: Subject kept", "Subject: Q3 budget follow-up" in text)
check("eml: body figure intact (no quoted-printable split)", "$18,950" in text)
check("eml: no raw MIME", "Content-Transfer-Encoding" not in text and "boundary=" not in text)

code, doc2 = put(inner_bytes, "application/octet-stream", "mail.eml")
check("eml as octet-stream: same text as message/rfc822", doc2 is not None and doc2["page_content"] == text)

code, doc = put(bytes(fwd), "message/rfc822", "fwd.eml")
text = doc["page_content"] if doc else ""
check("fwd: outer Cc kept", "Cc: finance@example.com" in text)
check("fwd: inner email headers kept", "Attached email:" in text and "From: Sam Client <sam@example.com>" in text)
check("fwd: xls attachment cells extracted", "--- Attachment: budget.xls ---" in text and "18950" in text)

code, doc = put(xls, "application/vnd.ms-excel", "budget.xls")
text = doc["page_content"] if doc else ""
check("xls: rows tab-separated", "\tTravel\t14740\t18950" in text)
check("xls: no email header block", "From:" not in text)

xls_text = text
code, doc = put(xls, "application/octet-stream", "budget.xls")
check("xls as octet-stream: same text as vnd.ms-excel", doc is not None and doc["page_content"] == xls_text)

# Open WebUI sends no Content-Type at all when the browser gave none; urllib would add one,
# so this goes through http.client to send exactly what Open WebUI sends.
import http.client, socket
host, port = LOADER.split("//")[1].split(":")
def raw(headers, body):
    try:
        c = http.client.HTTPConnection(host, int(port), timeout=60)
        c.putrequest("PUT", "/process", skip_accept_encoding=True)
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders(body)
        r = c.getresponse()
        return r.status, r.read()
    except Exception as e:  # a dropped connection is a failed check, not the end of the run
        return None, repr(e).encode()
code, body = raw({"Authorization": f"Bearer {KEY}", "X-Filename": "budget.xls", "Content-Length": str(len(xls))}, xls)
check("xls with no Content-Type: same text", code == 200 and json.loads(body)["page_content"] == xls_text)
code, doc = put(xls, "text/plain", "budget.xls")
check("xls as text/plain: same text (no binary garbage)", doc is not None and doc["page_content"] == xls_text)
code, doc = put(b"item,cost\nwidget,CSV-MARKER-7\n", "text/plain", "costs.csv")
check("csv as text/plain: still extracted (positive control)", doc is not None and "CSV-MARKER-7" in doc["page_content"])
code, _ = raw({"Authorization": f"Bearer {KEY}", "Content-Length": "abc"}, b"")
check("non-numeric Content-Length: 400", code == 400)

def send_raw(payload):
    s = socket.create_connection((host, int(port)), timeout=10)
    s.sendall(payload)
    try:
        reply = s.recv(1024)
    except Exception:
        reply = b""
    s.close()
    return reply
# Four words with an HTTP/1.1 version: rejected as bad syntax before command/path are set, the
# case the first version's log_message crashed on (it sent nothing). A version-less line would
# get an HTTP/0.9-style reply with no status line on some Python releases, so it isn't used.
reply = send_raw(b"GET /process HTTP/1.1 extra\r\n\r\n")
check("malformed request line: answered with 400", reply.startswith(b"HTTP/1.") and b" 400 " in reply.split(b"\r\n")[0])

# Declared 20 bytes, sent 5, then closed the sending side: must be refused, not extracted.
s = socket.create_connection((host, int(port)), timeout=10)
s.sendall(f"PUT /process HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {KEY}\r\nX-Filename: a.txt\r\n"
          f"Content-Type: text/plain\r\nContent-Length: 20\r\n\r\nhello".encode())
s.shutdown(socket.SHUT_WR)
reply = s.recv(1024); s.close()
check("short body: 400, not a partial extraction", b" 400 " in reply.split(b"\r\n")[0])

check("wrong key: 401", put(inner_bytes, "message/rfc822", "mail.eml", key="x" * 48)[0] == 401)
check("empty body: 413", put(b"", "message/rfc822", "mail.eml")[0] == 413)

sys.exit(1 if failures else 0)
EOF
then :; else rc=1; fi

logs="$(docker logs "$run-loader" 2>&1)"
if printf '%s' "$logs" | grep -q -e LEAK -e $'\e'; then
  echo "FAIL loader log: request target leaked"; rc=1
elif ! printf '%s' "$logs" | grep -q '^GET other -> 404$'; then
  echo "FAIL loader log: no line for the probe (positive control)"; rc=1
else
  echo "ok   loader log: probe logged as 'GET other -> 404', target not logged"
fi
exit "$rc"
