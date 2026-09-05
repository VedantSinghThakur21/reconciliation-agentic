from pathlib import Path

p = Path("src/agui/planner.py")
t = p.read_text(encoding="utf-8")
old = '''        req = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )'''
new = '''        req = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                # Cloudflare on api.groq.com returns 403/1010 without a UA.
                "User-Agent": "Mozilla/5.0 (compatible; reconq-core-planner/0.1)",
                "Accept": "application/json",
            },
            method="POST",
        )'''
if old not in t:
    raise SystemExit("headers block not found")
p.write_text(t.replace(old, new, 1), encoding="utf-8")
print("UA added")
