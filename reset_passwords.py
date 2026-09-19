"""Reset all users to temporary password ChangeMe123! (must change on login).
Run from project folder after: pip install -r requirements.txt
  python reset_passwords.py
"""
import sqlite3
from pathlib import Path

try:
    import bcrypt
except ImportError:
    raise SystemExit("Install bcrypt first:  pip install bcrypt")

TEMP = "ChangeMe123!"
DB = Path(__file__).resolve().parent / "cbmp.db"
h = bcrypt.hashpw(TEMP.encode(), bcrypt.gensalt()).decode()
conn = sqlite3.connect(DB)
cur = conn.cursor()
users = [r[0] for r in cur.execute("SELECT username FROM users").fetchall()]
if not users:
    print("No users yet. Start the app once to seed accounts, then run this again.")
else:
    for u in users:
        cur.execute(
            "UPDATE users SET password_hash=?, must_change=1 WHERE username=?",
            (h, u),
        )
    conn.commit()
    print("Updated:", ", ".join(users))
print()
print("Username examples: admin, partner, county, donor, supervisor, facility, viewer")
print("Temporary password:  ChangeMe123!")
print("You must set a new password on first login.")
conn.close()
