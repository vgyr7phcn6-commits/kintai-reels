# -*- coding: utf-8 -*-
"""置き場（このリポジトリ）の共通の道具：場所・日本時間・json の読み書き・Buffer への問い合わせ"""
import datetime, json, os, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
JST = datetime.timezone(datetime.timedelta(hours=9))
API = "https://api.buffer.com"


def path(*p):
    return os.path.join(ROOT, *p)


def load(p, default):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def save(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, p)


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def parse_ts(s):
    if not s:
        return None
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def jst_date(dt):
    return dt.astimezone(JST).date()


def api_key():
    """鍵は環境変数（GitHub の Secrets）か、手元ではファイル（BUFFER_API_KEY_FILE）から。画面には出さない"""
    k = os.environ.get("BUFFER_API_KEY", "").strip()
    if not k and os.environ.get("BUFFER_API_KEY_FILE"):
        with open(os.environ["BUFFER_API_KEY_FILE"], encoding="utf-8-sig") as fh:
            k = fh.read().strip()
    if not k:
        raise SystemExit("Buffer の鍵がありません（BUFFER_API_KEY）")
    return k


def gql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Authorization": "Bearer " + api_key(), "Content-Type": "application/json", "User-Agent": "kintai-reels/1"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError("Buffer HTTP %s: %s" % (e.code, e.read()[:800].decode("utf-8", "replace")))
    if res.get("errors"):
        raise RuntimeError("Buffer: " + json.dumps(res["errors"], ensure_ascii=False)[:1500])
    return res["data"]


def url_ok(url):
    """動画の URL がだれでも開けるか（HEAD で 200 と video/）"""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "kintai-reels/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200 and r.headers.get("Content-Type", "").startswith("video/")
    except Exception:
        return False
