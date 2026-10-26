# -*- coding: utf-8 -*-
"""集めた数字から「伸びる時間」と「伸びる動画」を出す。出す物：data/analysis.json と 統計.md

考え方：投稿の伸び（再生数の対数）＝ SNS ごとの基本 ＋ 動画の力 ＋ その SNS での時間の力。
同じ動画を3つの SNS に別々の時間で出しているので、動画の力と時間の力を分けて見られる。
数が少ないうちは 0 に寄せて（言い過ぎないように）、まわりから少しずつ決める。
"""
import datetime, math, os, statistics, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import JST, jst_date, load, now, parse_ts, path, save

VIEW_KEYS = ("views", "impressions", "reach", "viewers")        # 再生の数として使う物（先にある方。Buffer の PostMetricType）
ENGAGE_KEYS = ("comments", "shares", "saves", "reposts")        # これに likes と reactions の多い方を足す（同じ物を2回数えない）


def engage_of(m):
    return max(m.get("likes", 0), m.get("reactions", 0)) + sum(m.get(k, 0) for k in ENGAGE_KEYS)
NAMES = {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube"}
WEEK = "月火水木金土日"
MIN_AGE_H = 48            # 出してから2日たった物だけ比べる
TARGET_AGE_H = 72         # 3日目の数字で比べる
SHRINK = 2.0              # 0 に寄せる強さ（投稿数がこれくらいで半分信じる）


def pick(snaps):
    ok = [s for s in snaps if s["age_h"] >= MIN_AGE_H]
    if not ok:
        return None
    return min(ok, key=lambda s: abs(s["age_h"] - TARGET_AGE_H))


def views_of(m):
    for k in VIEW_KEYS:
        if k in m:
            return m[k], k
    return None, None


def fit(rows, iters=30):
    """rows: (channel, item, slot, y) → 基本・動画の力・時間の力（0 に寄せた平均で交互に決める）"""
    base = {c: statistics.mean(y for cc, _, _, y in rows if cc == c) for c in {r[0] for r in rows}}
    a, b = {}, {}
    for _ in range(iters):
        acc = {}
        for c, i, s, y in rows:
            acc.setdefault(i, []).append(y - base[c] - b.get((c, s), 0))
        a = {i: sum(v) / (len(v) + SHRINK) for i, v in acc.items()}
        acc = {}
        for c, i, s, y in rows:
            acc.setdefault((c, s), []).append(y - base[c] - a.get(i, 0))
        b = {k: sum(v) / (len(v) + SHRINK) for k, v in acc.items()}
        acc = {}
        for c, i, s, y in rows:
            acc.setdefault(c, []).append(y - a.get(i, 0) - b.get((c, s), 0))
        base = {c: statistics.mean(v) for c, v in acc.items()}
    return base, a, b


def pct(x):
    return "%+d%%" % round((math.exp(x) - 1) * 100)


def main():
    cfg = load(path("config.json"), {})
    items = {it["id"]: it for it in load(path("posts.json"), [])}
    ledger = load(path("data", "ledger.json"), {})
    metrics = load(path("data", "metrics.json"), {})
    t = now()
    rows, per_post = [], []
    for pid, e in ledger.items():
        s = pick(metrics.get(pid, []))
        if not s:
            continue
        v, vk = views_of(s["m"])
        if v is None:
            continue
        eng = engage_of(s["m"])
        sent = parse_ts(e.get("sentAt") or e["dueAt"])
        rows.append((e["channel"], e["item"], e["slot"], math.log1p(v)))
        per_post.append({"post": pid, "channel": e["channel"], "item": e["item"], "slot": e["slot"], "views": v, "view_key": vk,
                         "engage": eng, "rate": eng / v if v else 0, "age_h": s["age_h"], "m": s["m"],
                         "weekday": WEEK[sent.astimezone(JST).weekday()], "link": e.get("link")})

    out = {"updated": t.isoformat(), "posts_measured": len(rows), "slots": {}, "items": [], "groups": {}, "channels": {}}
    md = ["# キンタイ救急隊のリール 統計", "",
          "毎朝の係（GitHub Actions）が Buffer から数字を集めて作り直す。最終更新：%s（日本時間）" % t.astimezone(JST).strftime("%Y-%m-%d %H:%M"), ""]
    if not rows:
        md += ["まだ比べられる投稿がありません（出してから2日たった物で比べます）。", ""]
    else:
        base, a, b = fit(rows)
        slots = cfg["slots_jst"]
        md += ["## SNS ごとの合計（3日目の数字）", "", "| SNS | 投稿 | 再生の合計 | 1本あたり（中央） | 反応の割合 |", "|---|---|---|---|---|"]
        for c in cfg["channels"]:
            ps = [p for p in per_post if p["channel"] == c]
            if not ps:
                continue
            tot = sum(p["views"] for p in ps)
            med = statistics.median(p["views"] for p in ps)
            rate = sum(p["engage"] for p in ps) / tot if tot else 0
            out["channels"][c] = {"posts": len(ps), "views": tot, "median": med, "rate": rate}
            md.append("| %s | %d | %s | %s | %.1f%% |" % (NAMES.get(c, c), len(ps), f"{tot:,.0f}", f"{med:,.0f}", rate * 100))
        md += ["", "## 伸びる時間（その SNS のふつうと比べて）", "",
               "数が少ないうちは控えめに出る。3本ずつ試すまでは、毎日順番に時間を変えて出している。", ""]
        md += ["| SNS | " + " | ".join(slots) + " | いちばん |", "|---|" + "---|" * (len(slots) + 1)]
        for c in cfg["channels"]:
            n = {s: sum(1 for r in rows if r[0] == c and r[2] == s) for s in slots}
            eff = {s: b.get((c, s), 0.0) for s in slots}
            tried = [s for s in slots if n[s]]
            best = max(tried, key=lambda s: eff[s]) if tried else None
            out["slots"][c] = {"n": n, "effect": eff, "best": best}
            md.append("| %s | " % NAMES.get(c, c) + " | ".join(("%s（%d）" % (pct(eff[s]), n[s])) if n[s] else "－" for s in slots)
                      + " | %s |" % (best or "－"))
        ranked = sorted(a.items(), key=lambda kv: -kv[1])
        for i, x in ranked:
            it = items.get(i, {})
            ps = [p for p in per_post if p["item"] == i]
            out["items"].append({"id": i, "effect": x, "title": it.get("title"), "series": it.get("series"), "style": it.get("style"),
                                 "voice": it.get("voice"), "views": {p["channel"]: p["views"] for p in ps},
                                 "rate": sum(p["engage"] for p in ps) / max(1, sum(p["views"] for p in ps))})
        md += ["", "## 伸びた動画（時間と SNS の差を除いた、動画そのものの力）", "",
               "| 順位 | 動画 | 系統 | 力 | " + " | ".join(NAMES[c] for c in cfg["channels"]) + " |",
               "|---|---|---|---|" + "---|" * len(cfg["channels"])]
        for k, row in enumerate(out["items"], 1):
            md.append("| %d | %s %s | %s／%s | %s | " % (k, row["id"], row["title"] or "", row["series"] or "", row["style"] or "", pct(row["effect"]))
                      + " | ".join(f"{row['views'].get(c, 0):,.0f}" if c in row["views"] else "－" for c in cfg["channels"]) + " |")
        for g in ("series", "style", "hook_type", "theme", "voice", "made_by"):
            acc = {}
            for i, x in a.items():
                key = items.get(i, {}).get(g)
                if key:
                    acc.setdefault(str(key), []).append(x)
            if acc:
                out["groups"][g] = {k: {"n": len(v), "effect": sum(v) / len(v)} for k, v in acc.items()}
        label = {"series": "系統", "style": "作り（雰囲気）", "hook_type": "つかみの型", "theme": "話の中心", "voice": "声", "made_by": "作り手"}
        for g, d in out["groups"].items():
            md += ["", "## %sごとの平均" % label[g], "", "| %s | 本数 | 力 |" % label[g], "|---|---|---|"]
            for k, v in sorted(d.items(), key=lambda kv: -kv[1]["effect"]):
                md.append("| %s | %d | %s |" % (k, v["n"], pct(v["effect"])))
        wd = {}
        for p in per_post:
            wd.setdefault(p["weekday"], []).append(math.log1p(p["views"]) - base[p["channel"]] - a.get(p["item"], 0))
        if wd:
            md += ["", "## 曜日（参考・数が少ないうちは当てにしない）", "", "| " + " | ".join(WEEK) + " |", "|" + "---|" * 7,
                   "| " + " | ".join(("%s（%d）" % (pct(sum(wd[w]) / (len(wd[w]) + SHRINK)), len(wd[w]))) if w in wd else "－" for w in WEEK) + " |"]
    out["per_post"] = per_post
    save(path("data", "analysis.json"), out)
    with open(path("統計.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(md) + "\n")
    print("measured", len(rows))


if __name__ == "__main__":
    main()
