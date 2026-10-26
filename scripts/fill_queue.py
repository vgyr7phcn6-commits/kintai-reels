# -*- coding: utf-8 -*-
"""毎朝1回（GitHub Actions の daily）。Buffer の予約を埋め足して、投稿の数字（統計）を集める。

- 3つの SNS ごとに、予約が config の queue_target 本になるまで posts.json の順に足す（1日1本）
- 投稿の時間は、統計がたまるまでは 6 つの時間を順に試す（同じ日は SNS ごとに別の時間）。
  たまったら analysis.json の「伸びる時間」を多めに使い、ときどき別の時間も試す
- 3つとも出し終わって数日たった動画は置き場から消す（Buffer は投稿の時に写しを持っている）
- 失敗した投稿・動画が尽きそうな時は data/alert.txt に書く（ワークフローが赤くなって GitHub からメールが来る）

手元で試す：BUFFER_API_KEY_FILE=鍵のファイル python scripts/fill_queue.py --dry
"""
import datetime, hashlib, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import JST, gql, iso, jst_date, load, now, parse_ts, path, save, url_ok

DRY = "--dry" in sys.argv

# Buffer の名前（2026-10-27 に調べた物）
LIVE = ("scheduled", "sending")          # まだ出ていない予約
DONE = ("sent",)
ERROR = "error"

Q_ORG = "query { account { organizations { id name } } }"
Q_CHANNELS = """query($org: OrganizationId!) { channels(input: {organizationId: $org}) {
  id service name isDisconnected isLocked isQueuePaused } }"""
Q_POSTS = """query($org: OrganizationId!, $ch: [ChannelId!], $after: String) {
  posts(first: 50, after: $after, input: {organizationId: $org, filter: {channelIds: $ch}, sort: [{field: dueAt, direction: desc}]}) {
    edges { node { id status dueAt sentAt externalLink error { message } metrics { type name value unit } metricsUpdatedAt } }
    pageInfo { hasNextPage endCursor } } }"""
M_CREATE = """mutation($input: CreatePostInput!) { createPost(input: $input) {
  __typename ... on PostActionSuccess { post { id dueAt status } } ... on MutationError { message } } }"""


def fetch_posts(org, cid, pages=4):
    out, after = [], None
    for _ in range(pages):
        r = gql(Q_POSTS, {"org": org, "ch": [cid], "after": after})["posts"]
        out += [e["node"] for e in r["edges"] or []]
        if not r["pageInfo"]["hasNextPage"]:
            break
        after = r["pageInfo"]["endCursor"]
    return out


def hm(s):
    h, m = s.split(":")
    return datetime.time(int(h), int(m))


def choose_slot(name, day, cfg, analysis):
    """統計がたまるまでは順番に試す。たまったら伸びる時間を多めに（explore_rate の割合だけ別の時間）"""
    slots = cfg["slots_jst"]
    d = (day - datetime.date(2026, 1, 1)).days
    explore = slots[(d + cfg["slot_offset"].get(name, 0)) % len(slots)]
    a = (analysis.get("slots") or {}).get(name)
    if not a or not a.get("best") or min(a["n"].get(s, 0) for s in slots) < cfg["explore_min_per_slot"]:
        return explore, "試し"
    h = int(hashlib.sha256(("%s|%s" % (day, name)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    if h < cfg["explore_rate"]:
        return explore, "試し"
    return a["best"], "伸びる時間"


def next_due(last_due, name, cfg, analysis, t):
    day = max(datetime.date.fromisoformat(cfg["start_date"]), jst_date(t))
    if last_due:
        day = max(day, jst_date(last_due) + datetime.timedelta(days=1))
    while True:
        slot, why = choose_slot(name, day, cfg, analysis)
        due = datetime.datetime.combine(day, hm(slot), JST)
        if due > t + datetime.timedelta(minutes=45):
            return due, slot, why
        day += datetime.timedelta(days=1)


def post_input(name, cid, it, url, due, cfg):
    inp = {"channelId": cid, "text": it["caption"], "schedulingType": "automatic", "mode": "customScheduled",
           "dueAt": iso(due), "needsApproval": False, "source": "kintai-reels",
           "assets": [{"video": {"url": url, "metadata": {"thumbnailOffset": int(it.get("thumb_ms", cfg["thumb_ms"]))}}}]}
    ai = bool(it.get("ai_label"))
    if name == "instagram":
        inp["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True, "isAiGenerated": ai}}
    elif name == "youtube":
        inp["metadata"] = {"youtube": {"title": it["title"][:100], "categoryId": cfg["youtube_category"], "privacy": "public",
                                       "madeForKids": False, "notifySubscribers": True, "isAiGenerated": ai}}
    elif name == "tiktok":
        inp["metadata"] = {"tiktok": {"isAiGenerated": ai}}
    return inp


def snapshot(metrics, pid, p, t):
    """数字は Buffer が1日1回取ってくる。8日目までは毎日、そのあとは週1回、35日目まで残す"""
    sent = parse_ts(p.get("sentAt")) or parse_ts(p.get("dueAt"))
    at = parse_ts(p.get("metricsUpdatedAt")) or t
    if not sent:
        return
    age_h = (at - sent).total_seconds() / 3600
    if age_h < 0 or age_h > 24 * 35:
        return
    snap = {"at": jst_date(t).isoformat(), "age_h": round(age_h, 1), "m": {x["type"]: x["value"] for x in p["metrics"] or []}}
    snaps = metrics.setdefault(pid, [])
    if snaps and snaps[-1]["at"] == snap["at"]:
        snaps[-1] = snap
    elif not snaps or age_h <= 24 * 8 or age_h - snaps[-1]["age_h"] >= 24 * 6.5:
        snaps.append(snap)


def main():
    cfg = load(path("config.json"), {})
    items = load(path("posts.json"), [])
    ledger = load(path("data", "ledger.json"), {})      # Buffer の投稿 id → {item, channel, slot, dueAt, status, sentAt, link}
    metrics = load(path("data", "metrics.json"), {})    # Buffer の投稿 id → [{at, age_h, m}]
    analysis = load(path("data", "analysis.json"), {})
    pruned = load(path("data", "pruned.json"), {})       # 置き場から消した動画 → 日付（posts.json は手元の係だけが書く）
    t = now()
    alerts, log = [], []
    org = gql(Q_ORG)["account"]["organizations"][0]["id"]
    chans = {c["id"]: c for c in gql(Q_CHANNELS, {"org": org})["channels"]}
    status = {"updated": iso(t), "paused": bool(cfg.get("paused")), "channels": {}}
    by_id = {it["id"]: it for it in items}

    for name, cid in cfg["channels"].items():
        ch = chans.get(cid)
        if not ch or ch["isDisconnected"] or ch["isLocked"]:
            alerts.append("%s：Buffer とのつながりが切れています（Buffer でつなぎ直してください）" % name)
            continue
        posts = fetch_posts(org, cid)
        seen = set()
        for p in posts:
            e = ledger.get(p["id"])
            if not e:
                continue                                    # 手で入れた投稿は、数に入れるだけ
            seen.add(p["id"])
            old = e.get("status")
            e.update(status=p["status"], dueAt=p["dueAt"], sentAt=p.get("sentAt"), link=p.get("externalLink"))
            if p["status"] == ERROR and old != ERROR:
                msg = (p.get("error") or {}).get("message") or "理由は Buffer の画面で"
                alerts.append("%s：%s（%s）の投稿に失敗しました：%s" % (name, e["item"], by_id.get(e["item"], {}).get("title", ""), msg))
            if p["status"] in DONE and p.get("metrics") is not None:
                snapshot(metrics, p["id"], p, t)
        for pid, e in ledger.items():
            if e["channel"] == name and pid not in seen and e.get("status") in LIVE and parse_ts(e["dueAt"]) < t - datetime.timedelta(days=2):
                e["status"] = "gone"                        # Buffer で消された物

        live = [p for p in posts if p["status"] in LIVE]
        dues = [parse_ts(p["dueAt"]) for p in posts if p["dueAt"] and (p["status"] in LIVE or p["status"] in DONE)]
        last_due = max(dues) if dues else None
        used = {e["item"] for e in ledger.values() if e["channel"] == name}
        ready = [it for it in items if it.get("status") == "ready" and it["id"] not in used]
        added = []
        if cfg.get("paused") or ch["isQueuePaused"]:
            log.append("%s：止めてあるので足さない" % name)
        while not cfg.get("paused") and not ch["isQueuePaused"] and len(live) + len(added) < cfg["queue_target"] and ready:
            it = ready[0]
            url = cfg["base_url"] + it["file"]
            if not url_ok(url):
                log.append("%s：%s の動画がまだ開けない（置き場の反映待ち）。明日また試す" % (name, it["id"]))
                break
            due, slot, why = next_due(last_due, name, cfg, analysis, t)
            if DRY:
                pid = "dry-%s-%s" % (name, it["id"])
            else:
                r = gql(M_CREATE, {"input": post_input(name, cid, it, url, due, cfg)})["createPost"]
                if r["__typename"] != "PostActionSuccess":
                    alerts.append("%s：%s を予約できませんでした：%s" % (name, it["id"], r.get("message")))
                    break
                pid = r["post"]["id"]
                ledger[pid] = {"item": it["id"], "channel": name, "slot": slot, "why": why, "dueAt": iso(due),
                               "status": "scheduled", "created": iso(t)}
            ready.pop(0)
            added.append("%s %s %s（%s）" % (it["id"], due.astimezone(JST).strftime("%m/%d %H:%M"), it["title"], why))
            last_due = due
        n_live = len(live) + len(added)
        status["channels"][name] = {"scheduled": n_live, "remaining": len(ready),
                                    "last_due": iso(last_due) if last_due else None, "added": added}
        log.append("%s：予約 %d 本・まだ使っていない動画 %d 本%s" % (name, n_live, len(ready), "".join("\n  + " + a for a in added)))
        if not ready and n_live <= cfg["low_stock_days"] and not cfg.get("paused"):
            alerts.append("%s：予約があと %d 日分で、次の動画がありません（新しい動画を足してください）" % (name, n_live))

    # 3つとも出し終わって数日たった動画は、置き場から消す
    by_item = {}
    for e in ledger.values():
        by_item.setdefault(e["item"], {})[e["channel"]] = e
    for it in items:
        es = by_item.get(it["id"], {})
        if it["id"] in pruned or set(es) < set(cfg["channels"]):
            continue
        if all(e.get("status") in DONE + ("gone",) for e in es.values()):
            last = max(parse_ts(e.get("sentAt") or e["dueAt"]) for e in es.values())
            if t - last > datetime.timedelta(days=cfg["prune_after_days"]):
                f = path(*it["file"].split("/"))
                if os.path.exists(f) and not DRY:
                    os.remove(f)
                pruned[it["id"]] = jst_date(t).isoformat()
                log.append("置き場から消した：%s（3つとも投稿ずみ）" % it["id"])

    status["alerts"] = alerts
    print("\n".join(log + ["", "知らせ："] + (alerts or ["なし"])))
    if DRY:
        return
    save(path("data", "ledger.json"), ledger)
    save(path("data", "metrics.json"), metrics)
    save(path("data", "status.json"), status)
    save(path("data", "pruned.json"), pruned)
    lp = path("data", "log.txt")
    old = open(lp, encoding="utf-8").read().splitlines() if os.path.exists(lp) else []
    stamp = t.astimezone(JST).strftime("%Y-%m-%d %H:%M")
    with open(lp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join((old + [stamp + "  " + l.replace("\n", "\n" + " " * 18) for l in log + alerts])[-600:]) + "\n")
    ap = path("data", "alert.txt")
    if alerts:
        with open(ap, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(alerts) + "\n")
    elif os.path.exists(ap):
        os.remove(ap)


if __name__ == "__main__":
    main()
