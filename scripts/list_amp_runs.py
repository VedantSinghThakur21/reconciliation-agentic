import json
import sqlite3

c = sqlite3.connect("data/finance_controller.db")
c.row_factory = sqlite3.Row
rows = c.execute(
    "SELECT id, status, summary_json FROM flow_runs WHERE id LIKE 'AMP-%' ORDER BY started_at DESC LIMIT 8"
).fetchall()
for r in rows:
    s = json.loads(r["summary_json"] or "{}")
    print(r["id"], r["status"], s.get("kickoff_id"))
