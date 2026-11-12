#!/usr/bin/env python3
"""Calco: lee los feeds, filtra con un modelo pequeño y escribe docs/links.js.

Gasto en tokens: el modelo solo ve titulares que no ha visto antes (una línea por
titular) y devuelve solo los que se quedan. Sin ANTHROPIC_API_KEY funciona igual,
con reglas simples en lugar del modelo.
"""
import calendar
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import feedparser

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("CALCO_FEEDS", ROOT / "feeds.json"))
STATE = Path(os.environ.get("CALCO_STATE", ROOT / "state" / "seen.json"))
OUT = Path(os.environ.get("CALCO_OUT", ROOT / "docs" / "links.js"))
STATUS = OUT.parent / "status.json"

KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL = os.environ.get("CALCO_MODEL", "claude-haiku-5-5")
UA = "Calco/1.0 (lector de feeds personal)"
TOPICS = ["arte", "moda", "letras", "ideas", "tech"]
BATCH = 80          # titulares por llamada al modelo
MAX_NEW = 240       # titulares nuevos que se clasifican por ejecución
TOPS_EVERY_H = 3    # cada cuánto se pueden reelegir los titulares de la jornada

CRITERIA = """Eres el editor de Calco, un muro de enlaces personal de cultura, ideas y tecnología para una lectora de Madrid que lee en español, inglés, francés, italiano y alemán.
Se quedan: ensayo largo, crítica y reseña con firma y tesis propia (libros, arte, moda, cine, escena, música); ideas y política europea argumentadas; tecnología con análisis. Vale igual un buen texto conservador que uno progresista si argumenta.
Se van: agenda y estrenos sin crítica, listas, notas de prensa, cotilleo, sucesos, deportes, política partidista del día (sobre todo la estadounidense), tecnología de producto o financiación sin análisis, guerra cultural sin argumento, pódcast y vídeos."""


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(feed):
    """Devuelve (feed, entradas, error). Prueba la dirección principal y, si falla, las alternativas."""
    last = "sin entradas"
    for url in [feed["u"]] + feed.get("alt", []):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read(3_000_000)
            parsed = feedparser.parse(data)
            if parsed.entries:
                if url != feed["u"]:
                    log(f"ALTERNATIVA  {feed['n']}: funciona {url}")
                    feed = dict(feed, used=url)
                return feed, parsed.entries, None
            last = "sin entradas"
        except Exception as e:  # un feed caído no detiene la ejecución
            last = f"{type(e).__name__}: {e}"[:160]
    return feed, [], last


def clean(text, limit):
    text = unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    if t:
        return calendar.timegm(t)
    raw = (e.get("published") or e.get("updated") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            d = datetime.strptime(raw, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return int(d.timestamp())
        except ValueError:
            continue
    return None


def ask(system, user, max_tokens):
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        out = json.load(r)
    text = "".join(b.get("text", "") for b in out.get("content", []))
    usage = out.get("usage", {})
    log(f"modelo: {usage.get('input_tokens', '?')} tokens de entrada, {usage.get('output_tokens', '?')} de salida")
    a, b = text.find("["), text.rfind("]")
    return json.loads(text[a:b + 1])


def classify(items):
    """items: lista de dicts nuevos. Devuelve {url: {"k":0/1,"t":[...],"g":str}}."""
    result = {}
    if not KEY:
        for it in items:
            result[it["u"]] = {"k": 1, "t": it["t0"]}
        return result
    system = CRITERIA + """
Recibirás líneas «número | fuente | titular | entradilla». Devuelve SOLO un array JSON con los que se quedan, sin texto alrededor:
[{"i":número,"t":["tema"],"g":"traducción"}]
«t»: uno o dos temas de: arte, moda, letras, ideas, tech. «g»: solo si el titular está en alemán, su traducción breve al español; si no, omítelo. Lo que no devuelvas se descarta. Sé exigente: suele quedarse menos de un tercio."""
    for start in range(0, len(items), BATCH):
        chunk = items[start:start + BATCH]
        lines = [f"{n} | {it['s']} | {it['h']} | {it['x']}" for n, it in enumerate(chunk)]
        try:
            kept = ask(system, "\n".join(lines), 2500)
        except Exception as e:
            log("fallo del modelo, se aplican reglas en este lote:", e)
            for it in chunk:
                result[it["u"]] = {"k": 1, "t": it["t0"]}
            continue
        for it in chunk:
            result[it["u"]] = {"k": 0}
        for row in kept:
            try:
                it = chunk[int(row["i"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            topics = [t for t in row.get("t", []) if t in TOPICS][:2] or it["t0"]
            rec = {"k": 1, "t": topics}
            if row.get("g"):
                rec["g"] = clean(str(row["g"]), 160)
            result[it["u"]] = rec
    return result


def pick_tops(links, now):
    """Dos titulares de la jornada entre lo publicado en las últimas 24 h."""
    pool = [l for l in links if now - l["ts"] <= 24 * 3600] or links
    pool = pool[:60]
    if len(pool) <= 2:
        return [l["u"] for l in pool]
    if KEY:
        system = CRITERIA + """
Elige los DOS titulares de la jornada: las dos piezas más importantes o más logradas de hoy, de fuentes distintas y de temas distintos. Devuelve SOLO un array JSON con sus dos números, por ejemplo [3,17]."""
        lines = [f"{n} | {l['s']} | {l['h']}" for n, l in enumerate(pool)]
        try:
            ids = ask(system, "\n".join(lines), 30)
            tops = [pool[int(i)]["u"] for i in ids[:2]]
            if len(set(tops)) == 2:
                return tops
        except Exception as e:
            log("fallo al elegir titulares de la jornada:", e)
    tops, sources = [], set()
    for l in pool:  # sin modelo: los dos más recientes de fuentes «all» distintas
        if l.get("core") and l["s"] not in sources:
            tops.append(l["u"])
            sources.add(l["s"])
        if len(tops) == 2:
            break
    for l in pool:  # completa hasta dos si faltan
        if len(tops) < 2 and l["u"] not in tops:
            tops.append(l["u"])
    return tops


def main():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    seen = state.get("seen", {})
    now = time.time()
    window = cfg["window_hours"] * 3600
    block = [b.lower() for b in cfg.get("block", [])]

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(fetch, cfg["feeds"]))

    status, candidates, by_url = [], [], {}
    for feed, entries, err in results:
        fresh = 0
        for e in entries:
            url, title, ts = (e.get("link") or "").strip(), clean(e.get("title", ""), 220), entry_time(e)
            if not url or not title or ts is None:
                continue  # sin fecha y hora de publicación no entra
            if ts > now + 3600 or now - ts > window:
                continue
            blob = (url + " " + e.get("author", "")).lower().replace(" ", "")
            if any(b in blob for b in block) or url in by_url:
                continue
            item = {"u": url, "h": title, "s": feed["n"], "l": feed["l"], "ts": ts,
                    "r": feed.get("r", []), "t0": feed.get("t", []), "core": feed.get("m") == "all",
                    "x": clean(e.get("summary", ""), 160), "max": feed.get("max", cfg["max_per_source"])}
            by_url[url] = item
            candidates.append(item)
            fresh += 1
        status.append({"n": feed["n"], "u": feed.get("used", feed["u"]), "ok": err is None, "error": err, "entradas": len(entries), "recientes": fresh})
        if err:
            log(f"FEED CAÍDO  {feed['n']}: {err}  ({feed['u']})")

    new = [c for c in candidates if c["u"] not in seen]
    new.sort(key=lambda c: -c["ts"])
    new = new[:MAX_NEW]
    log(f"{len(candidates)} entradas en la ventana, {len(new)} nuevas para clasificar, modelo: {MODEL if KEY else 'ninguno (reglas)'}")
    verdicts = classify(new) if new else {}
    for url, rec in verdicts.items():
        rec["at"] = int(now)
        seen[url] = rec

    links, per_source, per_filter_feed = [], {}, {}
    for c in sorted(candidates, key=lambda c: -c["ts"]):
        rec = seen.get(c["u"])
        if not rec or not rec.get("k"):
            continue
        if per_source.get(c["s"], 0) >= c["max"]:
            continue
        if not KEY and not c["core"]:  # sin modelo, los feeds generalistas aportan poco
            if per_filter_feed.get(c["s"], 0) >= 1:
                continue
            per_filter_feed[c["s"]] = 1
        per_source[c["s"]] = per_source.get(c["s"], 0) + 1
        link = {"d": datetime.fromtimestamp(c["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "s": c["s"], "l": c["l"], "t": list(dict.fromkeys(c["r"] + rec.get("t", c["t0"]))),
                "h": c["h"], "u": c["u"], "ts": c["ts"], "core": c["core"]}
        if rec.get("g"):
            link["g"] = rec["g"]
        links.append(link)
    links = links[:cfg["max_links"]]

    urls = {l["u"] for l in links}
    tops = [u for u in state.get("tops", []) if u in urls]
    stale = now - state.get("tops_at", 0) > TOPS_EVERY_H * 3600
    if links and (len(tops) < 2 or (stale and verdicts)):
        tops = pick_tops(links, now)
        state["tops_at"] = int(now)
    state["tops"] = tops

    def public(l):
        return {k: v for k, v in l.items() if k not in ("ts", "core")}
    top_links = [public(l) for u in tops for l in links if l["u"] == u]
    rest = [public(l) for l in links if l["u"] not in tops]
    payload = {"updated": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "tops": top_links, "links": rest}

    if not links and OUT.exists():
        log("ningún enlace: se conserva la lista anterior")
    else:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text("window.CALCO = " + json.dumps(payload, ensure_ascii=False, indent=0) + ";\n", encoding="utf-8")
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")

    state["seen"] = {u: r for u, r in seen.items() if now - r.get("at", now) < 7 * 24 * 3600}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    down = [s["n"] for s in status if not s["ok"]]
    log(f"{len(links)} enlaces publicados, {len(tops)} titulares de la jornada, {len(down)} feeds caídos de {len(status)}")


if __name__ == "__main__":
    main()
