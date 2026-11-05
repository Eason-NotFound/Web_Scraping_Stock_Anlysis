"""Small, versioned SQLite store. Each method uses its own short transaction."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)

class Store:
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def init(self):
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript('''
              CREATE TABLE IF NOT EXISTS quotes(symbol TEXT, time TEXT, payload TEXT NOT NULL, PRIMARY KEY(symbol,time));
              CREATE TABLE IF NOT EXISTS bars(symbol TEXT, time TEXT, payload TEXT NOT NULL, PRIMARY KEY(symbol,time));
              CREATE TABLE IF NOT EXISTS news_versions(id TEXT, hash TEXT, updated TEXT, payload TEXT NOT NULL, analysis TEXT, PRIMARY KEY(id,hash));
              CREATE INDEX IF NOT EXISTS news_updated ON news_versions(updated);
              CREATE TABLE IF NOT EXISTS rules(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS alerts(event_id TEXT PRIMARY KEY, created TEXT, payload TEXT NOT NULL, delivery_status TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, level TEXT, message TEXT);
            ''')

    def assert_mode(self, mode):
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO state VALUES('data_mode',?)", (dump(mode),))
            existing = json.loads(c.execute("SELECT payload FROM state WHERE key='data_mode'").fetchone()[0])
            if existing != mode:
                raise ValueError("Database mode mismatch; use separate live/demo databases")

    def put_state(self, key, value):
        with self.connect() as c:
            c.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key,dump(value)))

    def get_state(self, key, default=None):
        with self.connect() as c:
            r = c.execute("SELECT payload FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(r[0]) if r else default

    def acquire_lease(self, owner, epoch, ttl=180):
        """Only one live worker; leases recover from a crashed process."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT payload FROM state WHERE key='worker_lease'").fetchone()
            old = json.loads(r[0]) if r else {}
            if old.get("owner") not in (None, owner) and old.get("expires",0)>epoch:
                return False
            c.execute("INSERT OR REPLACE INTO state VALUES('worker_lease',?)",(dump({"owner":owner,"expires":epoch+ttl}),))
            return True

    def release_lease(self, owner):
        with self.connect() as c:
            r=c.execute("SELECT payload FROM state WHERE key='worker_lease'").fetchone()
            if r and json.loads(r[0]).get("owner")==owner:
                c.execute("DELETE FROM state WHERE key='worker_lease'")

    def save_quote(self, q):
        self.assert_mode(q["data_mode"])
        if any(not math.isfinite(float(q[k])) or float(q[k])<=0 for k in ("price","previous_close")):
            raise ValueError("Prices must be finite and positive")
        with self.connect() as c:
            c.execute("INSERT INTO quotes VALUES(?,?,?) ON CONFLICT(symbol,time) DO UPDATE SET payload=excluded.payload",(q["symbol"],q["source_time"],dump(q)))

    def latest_quotes(self):
        with self.connect() as c:
            rows=c.execute("SELECT payload FROM quotes q WHERE time=(SELECT MAX(time) FROM quotes WHERE symbol=q.symbol) ORDER BY symbol").fetchall()
            return [json.loads(r[0]) for r in rows]

    def quote_history(self, symbol, limit=500):
        with self.connect() as c:
            rows=c.execute("SELECT payload FROM quotes WHERE symbol=? ORDER BY time DESC LIMIT ?",(symbol,limit)).fetchall()
            return [json.loads(r[0]) for r in reversed(rows)]

    def save_bars(self, bars):
        with self.connect() as c:
            for b in bars:
                if not math.isfinite(float(b["close"])) or float(b["close"])<=0:
                    raise ValueError("Invalid bar close")
                c.execute("INSERT OR REPLACE INTO bars VALUES(?,?,?)",(b["symbol"],b["time"],dump(b)))

    def bars(self, symbol):
        with self.connect() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT payload FROM bars WHERE symbol=? ORDER BY time",(symbol,))]

    def save_news(self, n):
        from .news_analysis import content_hash
        self.assert_mode(n["data_mode"])
        h=content_hash(n)
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO news_versions VALUES(?,?,?,?,NULL)",(str(n["id"]),h,n["updated_at"],dump(n)))
        return h

    def news_rows(self, limit=100):
        with self.connect() as c:
            rows=c.execute('''SELECT * FROM news_versions n WHERE rowid=(SELECT rowid FROM news_versions WHERE id=n.id ORDER BY updated DESC,rowid DESC LIMIT 1) ORDER BY updated DESC LIMIT ?''',(limit,)).fetchall()
            return [{**json.loads(r["payload"]),"hash":r["hash"],"analysis":json.loads(r["analysis"]) if r["analysis"] else None} for r in rows]

    def pending_news(self, limit=10):
        return [n for n in self.news_rows(500) if n["analysis"] is None or n["analysis"].get("status")=="error"][:limit]

    def save_analysis(self, news_id, hash, analysis):
        with self.connect() as c:
            c.execute("UPDATE news_versions SET analysis=? WHERE id=? AND hash=?",(dump(analysis),str(news_id),hash))

    def rules(self):
        with self.connect() as c:
            return [{**json.loads(r["payload"]),"id":r["id"]} for r in c.execute("SELECT * FROM rules ORDER BY id")]

    def upsert_rule(self, rule):
        r=dict(rule)
        rid=r.pop("id",None)
        if r.get("direction") not in {"above","below"} or not re.fullmatch(r"[A-Z.]{1,10}",r.get("symbol","")):
            raise ValueError("Invalid symbol or direction")
        threshold=float(r.get("threshold",0))
        if not math.isfinite(threshold) or threshold<=0:
            raise ValueError("Threshold must be finite and positive")
        r.update(threshold=threshold,cooldown_seconds=max(0,int(r.get("cooldown_seconds",1800))),enabled=bool(r.get("enabled",True)),require_news=bool(r.get("require_news",False)),categories=r.get("categories",[]))
        if any(x not in {"Finance","Politics & Policy","Macro","Other"} for x in r["categories"]):
            raise ValueError("Invalid news category")
        with self.connect() as c:
            if rid:
                changed=c.execute("UPDATE rules SET payload=? WHERE id=?",(dump(r),rid)).rowcount
                if not changed:
                    raise ValueError("Cannot update a rule that does not exist")
                c.execute("DELETE FROM state WHERE key=?",(f"baseline:{rid}",))
            else:
                rid=c.execute("INSERT INTO rules(payload) VALUES(?)",(dump(r),)).lastrowid
        return rid

    def disable_rule(self, rid):
        for r in self.rules():
            if r["id"]==rid:
                r["enabled"]=False
                self.upsert_rule(r)

    def delete_rule(self, rid):
        with self.connect() as c:
            c.execute("DELETE FROM rules WHERE id=?",(rid,))
            c.execute("DELETE FROM state WHERE key IN (?,?)",(f"baseline:{rid}",f"cooldown:{rid}"))

    def create_alert(self, a):
        self.assert_mode(a["data_mode"])
        a={**a,"delivery_status":a.get("delivery_status","pending")}
        with self.connect() as c:
            return c.execute("INSERT OR IGNORE INTO alerts VALUES(?,?,?,?)",(a["event_id"],a["created_at"],dump(a),a["delivery_status"])).rowcount==1

    def alerts(self, limit=100):
        with self.connect() as c:
            return [{**json.loads(r["payload"]),"delivery_status":r["delivery_status"]} for r in c.execute("SELECT * FROM alerts ORDER BY created DESC LIMIT ?",(limit,))]

    def pending_alerts(self):
        return [a for a in self.alerts(1000) if a["delivery_status"]=="pending"]

    def set_delivery(self, event_id, status, error=""):
        with self.connect() as c:
            row=c.execute("SELECT payload FROM alerts WHERE event_id=?",(event_id,)).fetchone()
            if row:
                payload=json.loads(row[0]); payload.update(delivery_error=error,delivery_updated_at=utcnow())
                c.execute("UPDATE alerts SET delivery_status=?,payload=? WHERE event_id=?",(status,dump(payload),event_id))

    def log(self, level, message):
        with self.connect() as c:
            c.execute("INSERT INTO logs(time,level,message) VALUES(?,?,?)",(utcnow(),level,str(message)[:1000]))

    def logs(self, limit=50):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT time,level,message FROM logs ORDER BY id DESC LIMIT ?",(limit,))]

    def counts(self):
        with self.connect() as c:
            return {t:c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("quotes","bars","news_versions","rules","alerts")}
