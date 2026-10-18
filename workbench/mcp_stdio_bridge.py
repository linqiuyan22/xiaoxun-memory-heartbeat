#!/usr/bin/env python3
"""Thin stdio-to-HTTP bridge for xiaoxun memory MCP.
CC spawns this; forwards JSONRPC stdin/stdout to HTTP MCP on loopback."""
import sys, json, os, urllib.request, urllib.error

URL = os.environ.get('XIAOXUN_MCP_URL', 'http://127.0.0.1:8765/mcp')
TOKEN = os.environ.get('XIAOXUN_MCP_TOKEN', '')

if not TOKEN:
    for p in [
        os.path.expanduser('~/.claude/channels/companion/.mcp_token'),
        '/etc/xiaoxun/mcp.env',
    ]:
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and line.startswith('XIAOXUN_MCP_TOKEN'):
                        TOKEN = line.split('=', 1)[1]
                        break
                    elif '=' not in line and len(line) >= 24:
                        TOKEN = line
                        break
            if TOKEN:
                break
        except OSError:
            pass

def forward(line: str) -> str:
    req = urllib.request.Request(
        URL,
        data=line.encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {TOKEN}',
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode('utf-8')

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            result = forward(line)
            sys.stdout.write(result + '\n')
            sys.stdout.flush()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode('utf-8', errors='replace')
            sys.stderr.write(f'HTTP {e.code}: {err_body}\n')
            sys.stderr.flush()
            try:
                req_obj = json.loads(line)
                err_resp = json.dumps({'jsonrpc': '2.0', 'id': req_obj.get('id'), 'error': {'code': -32603, 'message': f'HTTP {e.code}'}})
                sys.stdout.write(err_resp + '\n')
                sys.stdout.flush()
            except Exception:
                pass
        except Exception as e:
            sys.stderr.write(f'bridge error: {e}\n')
            sys.stderr.flush()

if __name__ == '__main__':
    main()
