"""API web de GEO Agent (Vercel, runtime Python)."""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
import uuid
import socket
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GEO_TIMEOUT", "10")   # fonctions serverless : on reste sous la limite

from flask import Flask, jsonify, request  # noqa: E402

import _geo as g  # noqa: E402

app = Flask(__name__)


# ── sécurité ──────────────────────────────────────────────

def auth_error():
    pwd = os.getenv("APP_PASSWORD", "")
    if not pwd:
        return "Définis la variable APP_PASSWORD dans Vercel avant d'utiliser l'outil."
    given = request.headers.get("X-App-Password", "")
    if not hmac.compare_digest(given.encode(), pwd.encode()):
        return "Mot de passe incorrect."
    return None


def public_url(url):
    """Refuse les adresses internes (localhost, réseau privé…)."""
    url = g.ensure_url((url or "").strip())
    host = urlparse(url).hostname
    if not host:
        raise g.ScanError("URL invalide.")
    if os.getenv("ALLOW_PRIVATE") == "1":
        return url
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise g.ScanError(f"Domaine introuvable : {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise g.ScanError("Adresse non autorisée.")
    return url


@app.before_request
def guard():
    if request.path.startswith("/api/") and request.path != "/api/ping" and not request.path.startswith("/api/public/"):
        err = auth_error()
        if err:
            return jsonify(error=err), 401


@app.errorhandler(g.ScanError)
def scan_error(e):
    return jsonify(error=str(e)), 400


@app.errorhandler(Exception)
def any_error(e):
    code = getattr(e, "code", 500)
    return jsonify(error=f"{type(e).__name__} : {e}"), code if isinstance(code, int) else 500


def body():
    return request.get_json(silent=True) or {}


def scan_payload(s):
    d = s.as_dict()
    d["final_url"] = s.final_url
    d["llms_txt"] = g.gen_llms(s)
    d["jsonld"] = g.gen_jsonld(s)
    return d


# ── routes ──────────────────────────────────────────────

# ── stockage (Upstash Redis, optionnel) ─────────────────────

def _redis_cfg():
    url = os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
    tok = os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN")
    return (url.rstrip("/"), tok) if url and tok else (None, None)


def redis(*cmd):
    """Exécute une commande Redis via l'API REST d'Upstash. Renvoie None si non configuré ou en erreur."""
    url, tok = _redis_cfg()
    if not url:
        return None
    try:
        r = g.requests.post(url, headers={"Authorization": f"Bearer {tok}"}, json=[str(c) for c in cmd], timeout=5)
        return r.json().get("result")
    except Exception as ex:  # noqa
        print("redis error:", ex)
        return None


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() or request.remote_addr or "?")


def rate_limited(kind, limit):
    """Limite par IP et par jour (actif seulement si Redis est configuré)."""
    ip_hash = hashlib.sha256(client_ip().encode()).hexdigest()[:16]
    key = f"rl:{kind}:{time.strftime('%Y%m%d')}:{ip_hash}"
    n = redis("INCR", key)
    if n == 1:
        redis("EXPIRE", key, 86400)
    return n is not None and int(n) > limit


def public_scan_payload(s):
    cats = []
    for cat in dict.fromkeys(c["cat"] for c in s.checks):
        cs = [c for c in s.checks if c["cat"] == cat]
        cats.append({"name": cat, "points": round(sum(c["points"] for c in cs), 1), "max": sum(c["max"] for c in cs)})
    plan = [{"label": c["label"], "gain": round(c["max"] - c["points"], 1), "fix": c["fix"]} for c in s.plan()]
    return cats, plan


# ── routes publiques (sans mot de passe) ─────────────────────

@app.get("/api/public/config")
def public_config():
    wa = re.sub(r"\D", "", os.getenv("WHATSAPP_NUMBER", "33663907888"))
    return jsonify(whatsapp=wa or None, email=os.getenv("CONTACT_EMAIL", "contact@presskit.fr"),
                   site=os.getenv("SITE_URL", "https://www.presskit.fr"))


@app.post("/api/public/scan")
def public_scan():
    if rate_limited("scan", int(os.getenv("PUBLIC_SCAN_LIMIT", "15"))):
        return jsonify(error="Tu as atteint la limite de tests pour aujourd'hui. Reviens demain."), 429
    s = g.Scan(public_url(body().get("url"))).run()
    cats, plan = public_scan_payload(s)
    sid = uuid.uuid4().hex
    redis("SET", f"scan:{sid}", json.dumps({"url": s.final_url, "score": s.score, "plan": plan}), "EX", 3600)
    return jsonify(id=sid, url=s.final_url, score=s.score, categories=cats,
                   top=plan[:3], remaining=max(0, len(plan) - 3))


@app.post("/api/public/lead")
def public_lead():
    b = body()
    name = (b.get("name") or "").strip()[:80]
    email = (b.get("email") or "").strip()[:120]
    phone = re.sub(r"[^\d+ ]", "", b.get("phone") or "")[:30].strip()
    activity = (b.get("activity") or "").strip()[:60]
    if not name or not (email or phone):
        raise g.ScanError("Indique ton prénom et ton e-mail ou ton numéro WhatsApp.")
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise g.ScanError("Cette adresse e-mail ne semble pas valide.")
    if not b.get("consent"):
        raise g.ScanError("Coche la case pour accepter d'être recontacté.")
    if rate_limited("lead", 20):
        return jsonify(error="Trop de demandes aujourd'hui. Réessaie demain."), 429
    cached = redis("GET", f"scan:{b.get('id', '')}")
    if cached:
        data = json.loads(cached)
    else:   # pas de cache : on relance l'audit
        s = g.Scan(public_url(b.get("url"))).run()
        data = {"url": s.final_url, "score": s.score, "plan": public_scan_payload(s)[1]}
    lead = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "name": name, "email": email, "phone": phone,
            "activity": activity, "url": data["url"], "score": data["score"],
            "issues": [p["label"] for p in data["plan"][:5]], "consent": True}
    saved = redis("LPUSH", "leads", json.dumps(lead, ensure_ascii=False)) is not None
    if not saved:
        print("NOUVEAU PROSPECT (stockage non configuré) :", json.dumps(lead, ensure_ascii=False))
    return jsonify(plan=data["plan"], score=data["score"], url=data["url"])


# ── routes privées ──────────────────────────────────────────

@app.get("/api/leads")
def leads():
    url, _ = _redis_cfg()
    if not url:
        return jsonify(leads=[], storage=False)
    raw = redis("LRANGE", "leads", 0, 499) or []
    return jsonify(leads=[json.loads(x) for x in raw], storage=True)


@app.get("/api/ping")
def ping():
    return jsonify(ok=True)


@app.get("/api/engines")
def engines():
    return jsonify(engines=[{"id": k, "label": v[0], "ready": bool(os.getenv(v[1]))} for k, v in g.ENGINES.items()],
                   analysis=bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("MISTRAL_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY")))


@app.post("/api/scan")
def scan():
    s = g.Scan(public_url(body().get("url"))).run()
    return jsonify(scan_payload(s))


@app.post("/api/pages")
def pages():
    s = g.Scan(public_url(body().get("url"))).run()
    rows = g.pages_test(s, 20)
    return jsonify(pages=[{"url": u, "status": st, "words": wc, "h1": h} for u, st, wc, h in rows])


@app.post("/api/analyse")
def analyse():
    s = g.Scan(public_url(body().get("url"))).run()
    text = g.agent_analyse(s)
    if text is None:
        return jsonify(error="Ajoute une clé ANTHROPIC_API_KEY, GEMINI_API_KEY, MISTRAL_API_KEY ou OPENAI_API_KEY dans Vercel."), 400
    return jsonify(text=text)


@app.post("/api/ask")
def ask():
    """Une question × un moteur (le téléphone lance les appels en parallèle)."""
    b = body()
    engine, question, cfg = b.get("engine"), (b.get("question") or "").strip(), b.get("config") or {}
    if engine not in g.ENGINES or not question or not cfg.get("brand"):
        raise g.ScanError("Paramètres manquants (moteur, question, marque).")
    label, key, fn = g.ENGINES[engine]
    if not os.getenv(key):
        raise g.ScanError(f"Pas de clé {key} configurée.")
    text, src = fn(question)
    src = list({x["url"] or x["title"]: x for x in src}.values())
    a = g.analyse_answer(cfg, text, src)
    return jsonify(engine=engine, label=label, question=question, answer=text, sources=src[:20], **a)


@app.post("/api/rediger")
def rediger():
    b = body()
    brand, domain, question = b.get("brand", ""), b.get("domain", ""), (b.get("question") or "").strip()
    if not brand or not question:
        raise g.ScanError("Indique la marque et la question.")
    ctx = ""
    if domain:
        r = g.fetch(public_url(domain))
        if r is not None:
            ctx = g.visible_text(g.BeautifulSoup(r.text, "html.parser"))[:5000]
    out = g.llm(f"Marque : {brand} ({domain})\nQuestion visée : {question}\n\n"
                f"Contenu actuel du site (extrait) :\n{ctx}\n\n"
                "Rédige une page web complète en Markdown qui répond à cette question pour qu'une IA puisse la citer : "
                "H1 = la question reformulée, réponse directe de 2-3 phrases en tête, puis sections H2, une liste, "
                "un tableau si pertinent, une FAQ de 4 questions, et un appel à l'action vers la marque. "
                "N'invente ni chiffres, ni clients, ni témoignages : mets [À COMPLÉTER] là où une donnée manque.",
                "Tu es rédacteur web spécialisé GEO. Tu écris en français, phrases courtes, tutoiement.")
    if out is None:
        return jsonify(error="Ajoute une clé ANTHROPIC_API_KEY, GEMINI_API_KEY, MISTRAL_API_KEY ou OPENAI_API_KEY dans Vercel."), 400
    return jsonify(text=out)


if __name__ == "__main__":   # test local : python3 api/index.py
    from flask import send_from_directory
    pub = os.path.join(os.path.dirname(__file__), "..", "public")

    @app.get("/")
    @app.get("/<path:p>")
    def static_files(p="index.html"):
        if p.endswith("/") or os.path.isdir(os.path.join(pub, p)):
            p = p.rstrip("/") + "/index.html"
        return send_from_directory(pub, p)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
