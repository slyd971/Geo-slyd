#!/usr/bin/env python3
"""
GEO Agent — audit GEO, suivi des citations dans les IA, générateurs JSON-LD / llms.txt.

Commandes :
  scan     URL                 Audit technique GEO (32 critères, score /100) + rapport HTML
  analyse  URL                 Scan + interprétation par une IA (plan d'action rédigé)
  pages    URL                 Test sans JavaScript de jusqu'à 20 pages du sitemap
  llms     URL                 Génère un llms.txt prêt à déposer à la racine du site
  jsonld   URL                 Génère le JSON-LD manquant (Organization, WebSite, FAQPage…)
  track    config.json         Pose tes questions suivies aux IA et enregistre les citations
  rapport  config.json         Rapport HTML d'évolution des citations (semaine par semaine)
  rediger  config.json "question"   Rédige une page pour une question où tu es absent

Clés API (variables d'environnement, toutes optionnelles) :
  OPENAI_API_KEY, PERPLEXITY_API_KEY, GEMINI_API_KEY, ANTHROPIC_API_KEY, MISTRAL_API_KEY
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; GEOAgent/1.0)"
HERE = os.path.dirname(os.path.abspath(__file__))

# Bots qui alimentent les réponses en temps réel (recherche / navigation)
SEARCH_BOTS = ["OAI-SearchBot", "ChatGPT-User", "PerplexityBot", "Claude-SearchBot",
               "Claude-User", "Googlebot", "Bingbot", "MistralAI-User"]
# Bots qui collectent pour l'entraînement des modèles
TRAINING_BOTS = ["GPTBot", "ClaudeBot", "Google-Extended", "CCBot", "Applebot-Extended"]

ENTITY_TYPES = {"organization", "localbusiness", "person", "musicgroup", "professionalservice",
                "corporation", "store", "performinggroup", "ngo", "brand", "onlinebusiness",
                "educationalorganization", "sportsorganization", "danceGroup".lower()}

MODELS = {
    "openai": os.getenv("GEO_OPENAI_MODEL", "gpt-5.4-mini"),
    "perplexity": os.getenv("GEO_PERPLEXITY_MODEL", "sonar"),
    "gemini": os.getenv("GEO_GEMINI_MODEL", "gemini-3.8-flash"),
    "anthropic": os.getenv("GEO_ANTHROPIC_MODEL", "claude-sonnet-5-5"),
    "mistral": os.getenv("GEO_MISTRAL_MODEL", "mistral-small-latest"),
}


# ───────────────────────────── utilitaires ─────────────────────────────

class ScanError(ValueError):
    pass


TIMEOUT = float(os.getenv("GEO_TIMEOUT", "20"))


def fetch(url, timeout=None):
    timeout = timeout or TIMEOUT
    try:
        t = time.time()
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "fr,en;q=0.8"},
                         timeout=timeout, allow_redirects=True)
        r.elapsed_s = time.time() - t
        if "charset" not in r.headers.get("content-type", "").lower():
            m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', r.content[:3000], re.I)
            r.encoding = m.group(1).decode() if m else (r.apparent_encoding or "utf-8")
        return r
    except requests.RequestException:
        return None


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return s.lower()


def words(text):
    return re.findall(r"[\wÀ-ÿ'-]+", text or "")


def visible_text(soup):
    s = BeautifulSoup(str(soup), "html.parser")
    for t in s(["script", "style", "noscript", "template", "svg", "head"]):
        t.decompose()
    return re.sub(r"\s+", " ", s.get_text(" ")).strip()


def flatten_jsonld(data):
    out = []
    if isinstance(data, list):
        for d in data:
            out += flatten_jsonld(d)
    elif isinstance(data, dict):
        out.append(data)
        for k in ("@graph", "mainEntity", "itemListElement"):
            if k in data:
                out += flatten_jsonld(data[k])
    return out


def types_of(obj):
    t = obj.get("@type", [])
    t = t if isinstance(t, list) else [t]
    return {str(x).lower() for x in t}


def brand_name(title, fallback=""):
    return (re.split(r"\s[|–—-]\s|\s\|", title or "")[0].strip() or fallback)


def site_root(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def ensure_url(url):
    return url if re.match(r"^https?://", url) else "https://" + url


# ───────────────────────────── scanner ─────────────────────────────

class Scan:
    def __init__(self, url):
        self.url = ensure_url(url)
        self.checks = []
        self.facts = {}

    def add(self, cid, cat, label, pmax, ratio, detail, fix):
        ratio = max(0.0, min(1.0, ratio))
        self.checks.append(dict(id=cid, cat=cat, label=label, max=pmax,
                                points=round(pmax * ratio, 1), ratio=ratio,
                                detail=detail, fix=fix if ratio < 1 else ""))

    @property
    def score(self):
        return round(sum(c["points"] for c in self.checks))

    def run(self):
        r = fetch(self.url)
        if r is None or r.status_code >= 400:
            raise ScanError(f"Impossible de charger {self.url} "
                             f"({'pas de réponse' if r is None else r.status_code})")
        self.final_url = r.url
        root = site_root(r.url)
        soup = BeautifulSoup(r.text, "html.parser")
        self.soup = soup
        text = visible_text(soup)
        self.facts.update(url=r.url, status=r.status_code, response_s=round(r.elapsed_s, 2),
                          words_no_js=len(words(text)))

        # ── 1. Accès des IA (25)
        rob = fetch(root + "/robots.txt")
        robots_txt = rob.text if rob is not None and rob.status_code == 200 and "html" not in rob.headers.get("content-type", "") else ""
        self.add("robots", "Accès des IA", "robots.txt présent", 2, 1 if robots_txt else 0,
                 "trouvé" if robots_txt else "absent",
                 "Crée un fichier /robots.txt (même minimal) avec la ligne Sitemap.")
        rp = robotparser.RobotFileParser()
        rp.parse(robots_txt.splitlines())
        def allowed(bot):
            return True if not robots_txt else rp.can_fetch(bot, r.url)
        blocked_s = [b for b in SEARCH_BOTS if not allowed(b)]
        blocked_t = [b for b in TRAINING_BOTS if not allowed(b)]
        self.facts.update(blocked_search_bots=blocked_s, blocked_training_bots=blocked_t)
        self.add("search_bots", "Accès des IA", "Bots de recherche IA autorisés", 8,
                 1 - len(blocked_s) / len(SEARCH_BOTS),
                 "tous autorisés" if not blocked_s else "bloqués : " + ", ".join(blocked_s),
                 "Autorise dans robots.txt : " + ", ".join(blocked_s) +
                 " (ce sont eux qui lisent ton site au moment où l'IA répond).")
        self.add("training_bots", "Accès des IA", "Bots d'entraînement autorisés", 3,
                 1 - len(blocked_t) / len(TRAINING_BOTS),
                 "tous autorisés" if not blocked_t else "bloqués : " + ", ".join(blocked_t),
                 "Choix stratégique : autoriser " + ", ".join(blocked_t) +
                 " aide les modèles à connaître ta marque à long terme.")

        llms = fetch(root + "/llms.txt")
        llms_ok = llms is not None and llms.status_code == 200 and llms.text.lstrip().startswith("#") \
            and "<html" not in llms.text[:500].lower()
        self.facts["llms_txt"] = llms_ok
        self.add("llms", "Accès des IA", "llms.txt présent et valide", 4, 1 if llms_ok else 0,
                 "valide" if llms_ok else "absent ou invalide",
                 "Dépose un /llms.txt (commande : geo_agent.py llms URL).")

        sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots_txt) or [root + "/sitemap.xml"]
        sm_ok, self.sitemap_urls = False, []
        for sm in sitemaps[:3]:
            s = fetch(sm)
            if s is not None and s.status_code == 200 and ("<urlset" in s.text or "<sitemapindex" in s.text):
                sm_ok = True
                self.sitemap_urls += self._read_sitemap(s.text)
        self.add("sitemap", "Accès des IA", "Sitemap XML accessible", 3, 1 if sm_ok else 0,
                 f"{len(self.sitemap_urls)} URL" if sm_ok else "introuvable",
                 "Publie un sitemap.xml et référence-le dans robots.txt.")
        https = r.url.startswith("https://")
        self.add("https", "Accès des IA", "HTTPS", 3, 1 if https else 0,
                 "oui" if https else "non", "Passe le site en HTTPS avec redirection 301.")
        rm = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
        robots_meta = rm.get("content", "") if rm else ""
        noindex = "noindex" in robots_meta.lower() or "noindex" in r.headers.get("X-Robots-Tag", "").lower()
        self.add("indexable", "Accès des IA", "Page indexable", 2, 0 if noindex else 1,
                 "noindex détecté" if noindex else "oui", "Retire la directive noindex.")

        # ── 2. Contenu lisible (21)
        wc = self.facts["words_no_js"]
        self.add("nojs_text", "Contenu lisible", "Texte lisible sans JavaScript (≥ 300 mots)", 8,
                 wc / 300, f"{wc} mots dans le HTML brut",
                 "Les IA lisent rarement le JavaScript : mets le texte principal dans le HTML "
                 "(rendu serveur ou génération statique) et vise 300 mots minimum.")
        h1 = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
        self.facts["h1"] = h1
        self.add("h1", "Contenu lisible", "Un seul H1", 3, 1 if len(h1) == 1 else 0,
                 f"{len(h1)} H1" + (f" : « {h1[0][:60]} »" if h1 else ""),
                 "Garde exactement un H1 qui dit qui tu es et ce que tu fais.")
        levels = [int(h.name[1]) for h in soup.find_all(re.compile("^h[1-6]$"))]
        jumps = sum(1 for a, b in zip(levels, levels[1:]) if b - a > 1)
        h2 = levels.count(2)
        self.facts["headings"] = [(h.name, h.get_text(" ", strip=True)[:80]) for h in soup.find_all(re.compile("^h[1-3]$"))][:40]
        self.add("hierarchy", "Contenu lisible", "Hiérarchie de titres propre", 3,
                 (0.5 if h2 >= 2 else 0) + (0.5 if jumps == 0 else 0),
                 f"{h2} H2, {jumps} saut(s) de niveau",
                 "Structure la page en sections H2 (≥ 2) sans sauter de niveau (H2 → H4).")
        questions = self._questions(soup)
        self.facts["questions_on_page"] = [q for q, _ in questions][:15]
        self.add("faq", "Contenu lisible", "Questions / réponses sur la page", 5,
                 len(questions) / 3, f"{len(questions)} question(s) détectée(s)",
                 "Ajoute une FAQ (3 questions minimum) avec les vraies questions de tes clients "
                 "en titres et une réponse courte et directe juste en dessous.")
        lists = len(soup.find_all(["ul", "ol"])) + 2 * len(soup.find_all("table"))
        self.add("lists", "Contenu lisible", "Listes ou tableaux extractibles", 2, lists / 2,
                 f"{lists} liste(s)/tableau(x)",
                 "Présente tes offres, étapes ou chiffres en listes ou tableaux : les IA les citent facilement.")

        # ── 3. Données structurées (22)
        raw_blocks = soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)})
        objs, invalid = [], 0
        for b in raw_blocks:
            try:
                objs += flatten_jsonld(json.loads(b.string or b.get_text() or ""))
            except (json.JSONDecodeError, TypeError):
                invalid += 1
        types = set().union(*[types_of(o) for o in objs]) if objs else set()
        self.jsonld, self.jsonld_types = objs, types
        self.facts["jsonld_types"] = sorted(types)
        self.add("jsonld", "Données structurées", "JSON-LD présent", 5, 1 if raw_blocks else 0,
                 f"{len(raw_blocks)} bloc(s)", "Ajoute du JSON-LD (commande : geo_agent.py jsonld URL).")
        self.add("jsonld_valid", "Données structurées", "JSON-LD valide", 3,
                 (1 if raw_blocks and not invalid else 0),
                 "aucun" if not raw_blocks else (f"{invalid} bloc(s) invalide(s)" if invalid else "valide"),
                 "Corrige la syntaxe JSON (virgules, guillemets) et vérifie sur validator.schema.org.")
        entity = types & ENTITY_TYPES or {t for t in types if t.endswith("business")}
        self.add("entity", "Données structurées", "Entité décrite (Organization / Person / LocalBusiness…)", 5,
                 1 if entity else 0, ", ".join(sorted(entity)) or "aucune",
                 "Décris qui tu es avec un bloc Organization, Person ou LocalBusiness (nom, logo, contact).")
        self.add("website", "Données structurées", "WebSite / WebPage", 2,
                 1 if types & {"website", "webpage", "aboutpage", "profilepage", "collectionpage"} else 0,
                 "oui" if types & {"website", "webpage"} else "non", "Ajoute un bloc WebSite avec name et url.")
        self.add("faqpage", "Données structurées", "FAQPage", 3, 1 if "faqpage" in types else 0,
                 "oui" if "faqpage" in types else "non",
                 "Balise ta FAQ avec FAQPage (le JSON-LD est généré par la commande jsonld).")
        same_as = [s for o in objs for s in (o.get("sameAs") if isinstance(o.get("sameAs"), list) else [o.get("sameAs")] if o.get("sameAs") else [])]
        self.add("sameas", "Données structurées", "sameAs (profils officiels liés)", 2, 1 if same_as else 0,
                 f"{len(same_as)} lien(s)", "Ajoute sameAs avec tes profils Instagram, LinkedIn, etc. "
                 "Ça aide les IA à relier ta marque à ses profils.")
        self.add("breadcrumb", "Données structurées", "BreadcrumbList", 2, 1 if "breadcrumblist" in types else 0,
                 "oui" if "breadcrumblist" in types else "non", "Ajoute un fil d'Ariane balisé BreadcrumbList.")

        # ── 4. Méta (18)
        title = (soup.title.get_text(strip=True) if soup.title else "")
        desc_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        desc = desc_tag.get("content", "").strip() if desc_tag else ""
        self.facts.update(title=title, description=desc)
        self.add("title", "Méta", "Balise title (30-65 caractères)", 3,
                 1 if 30 <= len(title) <= 65 else (0.5 if title else 0),
                 f"{len(title)} car. : « {title[:70]} »", "Écris un title de 30 à 65 caractères : marque + ce que tu fais + lieu.")
        self.add("description", "Méta", "Meta description (70-160 caractères)", 3,
                 1 if 70 <= len(desc) <= 160 else (0.5 if desc else 0),
                 f"{len(desc)} car.", "Écris une meta description de 70 à 160 caractères qui résume l'offre.")
        canon = soup.find("link", rel=lambda v: v and "canonical" in v)
        self.add("canonical", "Méta", "URL canonique", 2, 1 if canon and canon.get("href") else 0,
                 canon.get("href") if canon else "absente", "Ajoute <link rel=\"canonical\">.")
        lang = (soup.html.get("lang") if soup.html else None) or ""
        self.add("lang", "Méta", "Langue déclarée (lang)", 2, 1 if lang else 0, lang or "absente",
                 "Ajoute lang=\"fr\" sur la balise <html>.")
        og = {p: soup.find("meta", property=p) for p in ("og:title", "og:description", "og:image")}
        okog = sum(1 for v in og.values() if v and v.get("content"))
        self.add("og", "Méta", "Open Graph complet", 3, okog / 3, f"{okog}/3 balises",
                 "Complète og:title, og:description et og:image.")
        vp = soup.find("meta", attrs={"name": "viewport"})
        self.add("viewport", "Méta", "Adapté au mobile (viewport)", 2, 1 if vp else 0,
                 "oui" if vp else "non", "Ajoute <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">.")
        imgs = soup.find_all("img")
        with_alt = sum(1 for i in imgs if (i.get("alt") or "").strip())
        ratio = with_alt / len(imgs) if imgs else 1
        self.add("alt", "Méta", "Images avec texte alternatif (≥ 90 %)", 3, 1 if ratio >= 0.9 else ratio,
                 f"{with_alt}/{len(imgs)} images", "Ajoute un alt descriptif à chaque image.")

        # ── 5. Confiance (14)
        html_low = r.text.lower()
        has_mail = bool(re.search(r"mailto:|[\w.+-]+@[\w-]+\.[a-z]{2,}", html_low))
        has_tel = bool(re.search(r"tel:|(\+33|0)[1-9](?:[ .-]?\d{2}){4}", html_low))
        has_wa = "wa.me" in html_low or "whatsapp" in html_low
        contact = sum([has_mail, has_tel or has_wa])
        self.add("contact", "Confiance", "Coordonnées visibles", 3, contact / 2,
                 ", ".join(x for x, ok in [("e-mail", has_mail), ("téléphone", has_tel), ("WhatsApp", has_wa)] if ok) or "aucune",
                 "Affiche un e-mail et un téléphone ou WhatsApp en clair.")
        links = [(a.get("href") or "", a.get_text(" ", strip=True)) for a in soup.find_all("a")]
        legal = any(re.search(r"mention|legal|cgv|propos|about|contact", norm(h + " " + t)) for h, t in links)
        self.add("legal", "Confiance", "Pages À propos / mentions légales liées", 3, 1 if legal else 0,
                 "oui" if legal else "non", "Ajoute des liens vers À propos, Contact et Mentions légales.")
        socials = sorted({urlparse(h).netloc.replace("www.", "") for h, _ in links
                          if re.search(r"instagram|linkedin|facebook|tiktok|youtube|x\.com|twitter|soundcloud|spotify", h)})
        self.social_links = [h for h, _ in links if re.search(r"instagram|linkedin|facebook|tiktok|youtube|x\.com|twitter|soundcloud|spotify", h)]
        self.add("social", "Confiance", "Liens vers profils sociaux", 2, 1 if socials else 0,
                 ", ".join(socials) or "aucun", "Lie tes profils officiels (Instagram, LinkedIn…).")
        fresh = bool(soup.find("time") or re.search(r'"date(modified|published)"', html_low)
                     or soup.find("meta", property="article:modified_time")
                     or re.search(r"20(2[4-9]|3\d)", text[-3000:]))
        self.add("fresh", "Confiance", "Signal de fraîcheur (date)", 2, 1 if fresh else 0,
                 "oui" if fresh else "non", "Affiche une date de mise à jour et dateModified dans le JSON-LD.")
        rt = self.facts["response_s"]
        self.add("speed", "Confiance", "Temps de réponse < 1,5 s", 2, 1 if rt < 1.5 else (0.5 if rt < 3 else 0),
                 f"{rt} s", "Allège la page (images WebP, cache, CDN).")
        internal = {urljoin(r.url, h).split("#")[0] for h, _ in links
                    if h and not h.startswith(("mailto:", "tel:", "#", "javascript:"))
                    and urlparse(urljoin(r.url, h)).netloc == urlparse(r.url).netloc}
        self.internal_links = sorted(internal)
        self.add("internal", "Confiance", "Maillage interne (≥ 5 liens)", 2, len(internal) / 5,
                 f"{len(internal)} lien(s) interne(s)", "Relie la page à tes autres pages (offres, exemples, contact).")
        return self

    def _read_sitemap(self, xml, depth=0):
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
        if "<sitemapindex" in xml and depth < 1:
            out = []
            for l in locs[:5]:
                s = fetch(l)
                if s is not None and s.status_code == 200:
                    out += self._read_sitemap(s.text, depth + 1)
            return out
        return locs

    @staticmethod
    def _questions(soup):
        qs = []
        for h in soup.find_all(re.compile("^(h[2-6]|summary|dt|button)$")):
            t = h.get_text(" ", strip=True)
            if t.endswith("?") and 8 < len(t) < 200:
                nxt = h.find_next(["p", "dd", "div"])
                ans = nxt.get_text(" ", strip=True)[:600] if nxt else ""
                qs.append((t, ans))
        seen, out = set(), []
        for q, a in qs:
            if q not in seen:
                seen.add(q)
                out.append((q, a))
        return out

    def plan(self):
        return sorted([c for c in self.checks if c["ratio"] < 1], key=lambda c: c["points"] - c["max"])

    def as_dict(self):
        return dict(url=self.url, score=self.score, facts=self.facts, checks=self.checks,
                    plan=[dict(label=c["label"], gain=round(c["max"] - c["points"], 1), fix=c["fix"]) for c in self.plan()])


def print_scan(s):
    print(f"\nScore GEO : {s.score}/100  —  {s.final_url}\n")
    cat = None
    for c in s.checks:
        if c["cat"] != cat:
            cat = c["cat"]
            tot = sum(x["points"] for x in s.checks if x["cat"] == cat)
            mx = sum(x["max"] for x in s.checks if x["cat"] == cat)
            print(f"■ {cat}  ({tot:g}/{mx})")
        mark = "✓" if c["ratio"] >= 1 else ("~" if c["ratio"] > 0 else "✗")
        print(f"  {mark} {c['label']:<55} {c['points']:>4g}/{c['max']:<2}  {c['detail']}")
    print("\nPlan d'action (par points gagnés) :")
    for i, c in enumerate(s.plan()[:10], 1):
        print(f"  {i:>2}. +{c['max'] - c['points']:g} pt  {c['label']} — {c['fix']}")


# ───────────────────────────── générateurs ─────────────────────────────

def gen_llms(s):
    name = brand_name(s.facts["title"], urlparse(s.final_url).netloc)
    desc = s.facts["description"] or (s.facts["h1"][0] if s.facts["h1"] else "")
    urls = s.sitemap_urls[:40] or s.internal_links[:40]
    lines = [f"# {name}", "", f"> {desc}", ""]
    if s.facts["h1"]:
        lines += [s.facts["h1"][0], ""]
    lines += ["## Pages principales", ""]
    for u in urls:
        slug = urlparse(u).path.strip("/").replace("-", " ").replace("/", " › ") or "Accueil"
        lines.append(f"- [{slug[:1].upper() + slug[1:]}]({u})")
    if s.facts["questions_on_page"]:
        lines += ["", "## Questions fréquentes", ""]
        lines += [f"- {q}" for q in s.facts["questions_on_page"]]
    lines += ["", "## Contact", "", f"- Site : {site_root(s.final_url)}"]
    return "\n".join(lines) + "\n"


def gen_jsonld(s):
    root = site_root(s.final_url)
    name = brand_name(s.facts["title"], urlparse(s.final_url).netloc)
    soup = s.soup
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", str(soup))
    logo = soup.find("meta", property="og:image")
    blocks = []
    if not (s.jsonld_types & ENTITY_TYPES):
        org = {"@context": "https://schema.org", "@type": "Organization", "@id": root + "/#organization",
               "name": name, "url": root + "/", "description": s.facts["description"]}
        if logo and logo.get("content"):
            org["logo"] = urljoin(root, logo["content"])
        if email:
            org["email"] = email.group(0)
        if s.social_links:
            org["sameAs"] = sorted(set(s.social_links))
        blocks.append(org)
    if "website" not in s.jsonld_types:
        blocks.append({"@context": "https://schema.org", "@type": "WebSite", "@id": root + "/#website",
                       "name": name, "url": root + "/", "inLanguage": (soup.html.get("lang") if soup.html else None) or "fr",
                       "publisher": {"@id": root + "/#organization"}})
    qs = Scan._questions(soup)
    if qs and "faqpage" not in s.jsonld_types:
        blocks.append({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a or "À compléter"}}
            for q, a in qs[:10]]})
    if "breadcrumblist" not in s.jsonld_types and urlparse(s.final_url).path.strip("/"):
        parts = urlparse(s.final_url).path.strip("/").split("/")
        items = [{"@type": "ListItem", "position": 1, "name": "Accueil", "item": root + "/"}]
        for i, p in enumerate(parts, 2):
            items.append({"@type": "ListItem", "position": i, "name": p.replace("-", " ").capitalize(),
                          "item": root + "/" + "/".join(parts[:i - 1])})
        blocks.append({"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": items})
    return "\n\n".join('<script type="application/ld+json">\n' + json.dumps(b, ensure_ascii=False, indent=2) + "\n</script>"
                       for b in blocks) or "<!-- Rien à ajouter : les blocs principaux sont déjà présents. -->"


def pages_test(s, limit=20):
    from concurrent.futures import ThreadPoolExecutor
    urls = (s.sitemap_urls or s.internal_links)[:limit]

    def one(u):
        r = fetch(u)
        if r is None:
            return (u, "erreur", 0, "")
        soup = BeautifulSoup(r.text, "html.parser")
        wc = len(words(visible_text(soup)))
        h1 = soup.find("h1")
        return (u, r.status_code, wc, h1.get_text(" ", strip=True)[:50] if h1 else "— pas de H1")
    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(one, urls))


# ───────────────────────────── IA : appels ─────────────────────────────

def _post(url, headers, body, timeout=120):
    r = requests.post(url, headers=headers, json=body, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.text[:300]}")
    return r.json()


def ask_openai(q):
    d = _post("https://api.openai.com/v1/responses",
              {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
              {"model": MODELS["openai"], "input": q, "tools": [{"type": "web_search"}]})
    return parse_openai(d)


def parse_openai(d):
    text, src = [], []
    for item in d.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text.append(c.get("text", ""))
                    src += [{"url": a.get("url", ""), "title": a.get("title", "")}
                            for a in c.get("annotations", []) if a.get("type") == "url_citation"]
    return "\n".join(text), src


def ask_perplexity(q):
    d = _post("https://api.perplexity.ai/chat/completions",
              {"Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}"},
              {"model": MODELS["perplexity"], "messages": [{"role": "user", "content": q}]})
    return parse_perplexity(d)


def parse_perplexity(d):
    text = d["choices"][0]["message"]["content"]
    src = [{"url": x.get("url", ""), "title": x.get("title", "")} for x in d.get("search_results", [])]
    if not src:
        src = [{"url": u, "title": ""} for u in d.get("citations", [])]
    return text, src


def ask_gemini(q):
    d = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{MODELS['gemini']}:generateContent",
              {"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
              {"contents": [{"parts": [{"text": q}]}], "tools": [{"google_search": {}}]})
    return parse_gemini(d)


def parse_gemini(d):
    cand = (d.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    chunks = cand.get("groundingMetadata", {}).get("groundingChunks", [])
    # Gemini renvoie des URL de redirection : le domaine réel est dans "title"
    return text, [{"url": c.get("web", {}).get("uri", ""), "title": c.get("web", {}).get("title", "")} for c in chunks]


def ask_anthropic(q, web=True, system=None, max_tokens=2000):
    body = {"model": MODELS["anthropic"], "max_tokens": max_tokens, "messages": [{"role": "user", "content": q}]}
    if web:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    if system:
        body["system"] = system
    d = _post("https://api.anthropic.com/v1/messages",
              {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"}, body)
    return parse_anthropic(d)


def parse_anthropic(d):
    text, src = [], []
    for b in d.get("content", []):
        if b.get("type") == "text":
            text.append(b.get("text", ""))
            src += [{"url": c.get("url", ""), "title": c.get("title", "")} for c in b.get("citations") or [] if c.get("url")]
        elif b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
            src += [{"url": x.get("url", ""), "title": x.get("title", "")} for x in b["content"]]
    return "".join(text), src


def ask_mistral(q, system=None):
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": q}]
    d = _post("https://api.mistral.ai/v1/chat/completions",
              {"Authorization": f"Bearer {os.environ['MISTRAL_API_KEY']}"},
              {"model": MODELS["mistral"], "messages": msgs})
    return d["choices"][0]["message"]["content"], []   # pas de recherche web via cette API


ENGINES = {"openai": ("ChatGPT", "OPENAI_API_KEY", ask_openai),
           "perplexity": ("Perplexity", "PERPLEXITY_API_KEY", ask_perplexity),
           "gemini": ("Gemini", "GEMINI_API_KEY", ask_gemini),
           "anthropic": ("Claude", "ANTHROPIC_API_KEY", ask_anthropic),
           "mistral": ("Le Chat (sans web)", "MISTRAL_API_KEY", ask_mistral)}


def llm(prompt, system):
    """Modèle d'analyse : essaie Claude, Gemini, Mistral puis OpenAI ; passe au suivant si l'un échoue."""
    pref = os.getenv("GEO_ANALYSIS", "")
    order = ["anthropic", "gemini", "mistral", "openai"]
    if pref in order:
        order.remove(pref)
        order.insert(0, pref)
    errors = []
    for p in order:
        key = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY",
               "mistral": "MISTRAL_API_KEY", "openai": "OPENAI_API_KEY"}[p]
        if not os.getenv(key):
            continue
        try:
            if p == "anthropic":
                return ask_anthropic(prompt, web=False, system=system, max_tokens=4000)[0]
            if p == "mistral":
                return ask_mistral(prompt, system=system)[0]
            if p == "gemini":
                d = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{MODELS['gemini']}:generateContent",
                          {"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                          {"systemInstruction": {"parts": [{"text": system}]},
                           "contents": [{"parts": [{"text": prompt}]}]})
                return parse_gemini(d)[0]
            if p == "openai":
                d = _post("https://api.openai.com/v1/responses",
                          {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                          {"model": MODELS["openai"], "instructions": system, "input": prompt})
                return parse_openai(d)[0]
        except Exception as ex:  # noqa
            errors.append(f"{ENGINES[p][0]} : {str(ex)[:200]}")
    if errors:
        raise RuntimeError("Aucune IA n'a répondu. " + " | ".join(errors))
    return None


# ───────────────────────────── suivi des citations ─────────────────────────────

def analyse_answer(cfg, text, sources):
    brand_terms = [cfg["brand"]] + cfg.get("aliases", [])
    nt = norm(text)
    pos = lambda terms: min([nt.find(norm(t)) for t in terms if norm(t) in nt] or [-1])
    bpos = pos(brand_terms)
    mentioned = bpos >= 0
    dom = norm(cfg.get("domain", ""))
    cited = bool(dom) and any(dom in norm(s["url"]) or dom in norm(s["title"]) for s in sources)
    comps = {}
    for c in cfg.get("competitors", []):
        names = c if isinstance(c, list) else [c]
        p = pos(names)
        if p >= 0:
            comps[names[0]] = p
    rank = (1 + sum(1 for p in comps.values() if p < bpos)) if mentioned else None
    tone = None
    if mentioned:
        i = max(0, bpos - 300)
        ctx = text[i:i + 700]
        res = llm(f"Marque : {cfg['brand']}\nExtrait d'une réponse d'IA :\n{ctx}\n\n"
                  "Réponds par un seul mot : positif, neutre ou négatif.", "Tu classes le ton envers une marque.")
        if res:
            m = re.search(r"positif|neutre|negatif", norm(res))
            tone = {"negatif": "négatif"}.get(m.group(0), m.group(0)) if m else None
    return dict(mentioned=mentioned, cited=cited, rank=rank, competitors=sorted(comps, key=comps.get), tone=tone)


def db(path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts TEXT, week TEXT, engine TEXT,
        question TEXT, mentioned INT, cited INT, rank INT, competitors TEXT, sources TEXT, tone TEXT, answer TEXT)""")
    return con


def track(cfg_path):
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    con = db(cfg.get("db", os.path.splitext(cfg_path)[0] + ".sqlite"))
    now = dt.datetime.now()
    week = f"{now.isocalendar()[0]}-S{now.isocalendar()[1]:02d}"
    engines = [e for e in cfg.get("engines", list(ENGINES)) if os.getenv(ENGINES[e][1])]
    skipped = [e for e in cfg.get("engines", list(ENGINES)) if e not in engines]
    if skipped:
        print("Moteurs ignorés (clé API absente) : " + ", ".join(ENGINES[e][0] for e in skipped))
    if not engines:
        raise SystemExit("Aucune clé API configurée. Voir le README.")
    for q in cfg["questions"]:
        for e in engines:
            label, _, fn = ENGINES[e]
            try:
                text, src = fn(q)
            except Exception as ex:  # noqa
                print(f"  ✗ {label} — {q[:50]} : {ex}")
                continue
            src = list({x["url"] or x["title"]: x for x in src}.values())
            a = analyse_answer(cfg, text, src)
            con.execute("INSERT INTO runs(ts,week,engine,question,mentioned,cited,rank,competitors,sources,tone,answer)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (now.isoformat(timespec="seconds"), week, e, q, int(a["mentioned"]), int(a["cited"]), a["rank"],
                         json.dumps(a["competitors"], ensure_ascii=False), json.dumps(src, ensure_ascii=False), a["tone"], text))
            con.commit()
            flag = ", ".join(x for x in [f"mentionné #{a['rank']}" if a["mentioned"] else "",
                                         "source citée" if a["cited"] else ""] if x) or "absent"
            print(f"  {label:<20} {flag:<22} {q[:60]}")
    print(f"\nEnregistré ({week}). Rapport : python3 geo_agent.py rapport {cfg_path}")


# ───────────────────────────── rapports HTML ─────────────────────────────

CSS = """body{font:15px/1.5 -apple-system,Segoe UI,Inter,sans-serif;max-width:980px;margin:40px auto;padding:0 20px;color:#111;background:#fafafa}
h1{font-size:26px;margin:0}h2{font-size:18px;margin-top:36px;border-bottom:1px solid #ddd;padding-bottom:6px}
.score{font-size:64px;font-weight:700;line-height:1}.muted{color:#666}table{border-collapse:collapse;width:100%;font-size:14px;background:#fff}
td,th{padding:7px 10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}th{background:#f3f3f3}
.ok{color:#0a7a3b}.ko{color:#c0362c}.mid{color:#b7791f}.bar{height:8px;background:#eee;border-radius:4px}.bar i{display:block;height:8px;border-radius:4px;background:#111}
pre{background:#111;color:#eee;padding:14px;overflow:auto;border-radius:6px;font-size:12px}.ai{background:#fff;border:1px solid #ddd;padding:16px 20px;border-radius:6px;white-space:pre-wrap}"""


def scan_html(s, ai_text=None, pages=None):
    e = html.escape
    rows, cat = [], None
    for c in s.checks:
        if c["cat"] != cat:
            cat = c["cat"]
            tot = sum(x["points"] for x in s.checks if x["cat"] == cat)
            mx = sum(x["max"] for x in s.checks if x["cat"] == cat)
            rows.append(f"<tr><th colspan=3>{e(cat)}</th><th>{tot:g}/{mx}</th></tr>")
        cls = "ok" if c["ratio"] >= 1 else ("mid" if c["ratio"] > 0 else "ko")
        rows.append(f"<tr><td class={cls}>{'✓' if cls == 'ok' else ('~' if cls == 'mid' else '✗')}</td>"
                    f"<td>{e(c['label'])}</td><td class=muted>{e(str(c['detail']))}</td><td>{c['points']:g}/{c['max']}</td></tr>")
    plan = "".join(f"<tr><td>+{c['max'] - c['points']:g}</td><td><b>{e(c['label'])}</b><br>{e(c['fix'])}</td></tr>" for c in s.plan())
    pg = ""
    if pages:
        pg = "<h2>Test sans JavaScript</h2><table><tr><th>Page</th><th>HTTP</th><th>Mots</th><th>H1</th></tr>" + "".join(
            f"<tr><td>{e(u)}</td><td>{st}</td><td class={'ok' if wc >= 300 else 'ko'}>{wc}</td><td>{e(h)}</td></tr>" for u, st, wc, h in pages) + "</table>"
    ai = f"<h2>Analyse et recommandations</h2><div class=ai>{e(ai_text)}</div>" if ai_text else ""
    return f"""<!doctype html><html lang=fr><meta charset=utf-8><title>Audit GEO — {e(s.final_url)}</title><style>{CSS}</style>
<h1>Audit GEO</h1><p class=muted>{e(s.final_url)} · {dt.date.today():%d/%m/%Y}</p>
<div class=score>{s.score}<span class=muted style="font-size:24px">/100</span></div>
<div class=bar style="margin:10px 0 0"><i style="width:{s.score}%"></i></div>
<h2>Plan d'action priorisé</h2><table><tr><th>Points</th><th>Action</th></tr>{plan or '<tr><td colspan=2>Rien à corriger.</td></tr>'}</table>
{ai}<h2>Détail des 32 critères</h2><table>{''.join(rows)}</table>{pg}
<p class=muted style="margin-top:40px">Mesures factuelles d'abord, interprétation ensuite. Aucun score ne garantit une citation dans les réponses des IA.</p></html>"""


def report(cfg_path):
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    con = db(cfg.get("db", os.path.splitext(cfg_path)[0] + ".sqlite"))
    e = html.escape
    weeks = [w for (w,) in con.execute("SELECT DISTINCT week FROM runs ORDER BY week")]
    if not weeks:
        raise SystemExit("Aucune donnée : lance d'abord la commande track.")
    engines = [x for (x,) in con.execute("SELECT DISTINCT engine FROM runs")]
    def pct(where, args):
        n, m, c = con.execute(f"SELECT COUNT(*), SUM(mentioned), SUM(cited) FROM runs WHERE {where}", args).fetchone()
        return (round(100 * (m or 0) / n) if n else 0, round(100 * (c or 0) / n) if n else 0, n)
    trend = "".join("<tr><td>" + w + "</td>" + "".join(
        "<td>{}% · {}%</td>".format(*pct("week=? AND engine=?", (w, en))[:2]) for en in engines) +
        "<td><b>{}% · {}%</b></td></tr>".format(*pct("week=?", (w,))[:2]) for w in weeks)
    last = weeks[-1]
    qrows = []
    for q in cfg["questions"]:
        cells = []
        for en in engines:
            r = con.execute("SELECT mentioned, cited, rank, tone FROM runs WHERE week=? AND engine=? AND question=? ORDER BY id DESC LIMIT 1",
                            (last, en, q)).fetchone()
            if not r:
                cells.append("<td class=muted>—</td>")
            elif r[0] or r[1]:
                cells.append(f"<td class=ok>{'#' + str(r[2]) if r[2] else ''}{' cité' if r[1] else ''}{' · ' + r[3] if r[3] else ''}</td>")
            else:
                cells.append("<td class=ko>absent</td>")
        qrows.append(f"<tr><td>{e(q)}</td>{''.join(cells)}</tr>")
    comp = {}
    for (cj,) in con.execute("SELECT competitors FROM runs WHERE week=?", (last,)):
        for c in json.loads(cj or "[]"):
            comp[c] = comp.get(c, 0) + 1
    total = con.execute("SELECT COUNT(*), SUM(mentioned) FROM runs WHERE week=?", (last,)).fetchone()
    sov = [(cfg["brand"], total[1] or 0)] + sorted(comp.items(), key=lambda x: -x[1])
    sov_rows = "".join(f"<tr><td>{e(n)}</td><td>{v}/{total[0]}</td><td><div class=bar><i style='width:{100 * v // max(total[0], 1)}%'></i></div></td></tr>" for n, v in sov)
    domains = {}
    for (sj,) in con.execute("SELECT sources FROM runs WHERE week=?", (last,)):
        for s in json.loads(sj or "[]"):
            d = (s.get("title") if "vertexaisearch" in s.get("url", "") else urlparse(s.get("url", "")).netloc).replace("www.", "")
            if d:
                domains[d] = domains.get(d, 0) + 1
    dom_rows = "".join(f"<tr><td>{e(d)}</td><td>{n}</td></tr>" for d, n in sorted(domains.items(), key=lambda x: -x[1])[:15])
    hdr = "".join(f"<th>{ENGINES.get(en, (en,))[0]}</th>" for en in engines)
    out = f"""<!doctype html><html lang=fr><meta charset=utf-8><title>Citations IA — {e(cfg['brand'])}</title><style>{CSS}</style>
<h1>Visibilité dans les IA — {e(cfg['brand'])}</h1><p class=muted>{len(weeks)} semaine(s) suivie(s) · dernière : {last}</p>
<h2>Évolution (mentionné · cité comme source)</h2><table><tr><th>Semaine</th>{hdr}<th>Total</th></tr>{trend}</table>
<h2>Questions suivies — {last}</h2><table><tr><th>Question</th>{hdr}</tr>{''.join(qrows)}</table>
<h2>Part de voix — {last}</h2><table><tr><th>Marque</th><th>Réponses</th><th></th></tr>{sov_rows}</table>
<h2>Sources les plus citées par les IA</h2><table><tr><th>Domaine</th><th>Citations</th></tr>{dom_rows}</table>
<p class=muted style="margin-top:40px">Les réponses des IA varient d'un essai à l'autre : regarde la tendance sur plusieurs semaines.</p></html>"""
    path = os.path.splitext(cfg_path)[0] + "-rapport.html"
    open(path, "w", encoding="utf-8").write(out)
    print("Rapport :", path)


# ───────────────────────────── agent d'analyse ─────────────────────────────

SYSTEM_ANALYSE = """Tu es un consultant GEO (Generative Engine Optimization) senior. On te donne les mesures
factuelles d'un audit technique. Rédige en français, en tutoyant, un plan d'action concret :
1. Diagnostic en 3 phrases (ce qui empêche les IA de citer ce site).
2. Les 5 actions prioritaires, chacune avec : pourquoi, quoi faire exactement, gain estimé en points.
3. 5 questions que les clients de ce site posent probablement aux IA, et la page ou section à créer pour chacune.
4. Les textes prêts à coller : title, meta description, et un paragraphe d'introduction de 60 mots maximum.
Ne t'appuie que sur les mesures fournies. N'invente ni chiffres, ni résultats garantis."""


def agent_analyse(s):
    facts = s.as_dict()
    facts["checks"] = [{k: c[k] for k in ("label", "points", "max", "detail")} for c in s.checks]
    body = visible_text(s.soup)[:6000]
    return llm("MESURES :\n" + json.dumps(facts, ensure_ascii=False, indent=1) + "\n\nTEXTE DE LA PAGE (extrait) :\n" + body,
               SYSTEM_ANALYSE)


def rediger(cfg_path, question):
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    ctx = ""
    if cfg.get("domain"):
        r = fetch(ensure_url(cfg["domain"]))
        if r is not None:
            ctx = visible_text(BeautifulSoup(r.text, "html.parser"))[:5000]
    out = llm(f"Marque : {cfg['brand']} ({cfg.get('domain', '')})\nQuestion visée : {question}\n\n"
              f"Contenu actuel du site (extrait) :\n{ctx}\n\n"
              "Rédige une page web complète en Markdown qui répond à cette question pour qu'une IA puisse la citer : "
              "H1 = la question reformulée, réponse directe de 2-3 phrases en tête, puis sections H2, une liste, "
              "un tableau si pertinent, une FAQ de 4 questions, et un appel à l'action vers la marque. "
              "N'invente ni chiffres, ni clients, ni témoignages : mets [À COMPLÉTER] là où une donnée manque.",
              "Tu es rédacteur web spécialisé GEO. Tu écris en français, phrases courtes, tutoiement.")
    if out is None:
        raise SystemExit("Cette commande a besoin d'une clé ANTHROPIC_API_KEY, MISTRAL_API_KEY ou OPENAI_API_KEY.")
    slug = re.sub(r"[^a-z0-9]+", "-", norm(question)).strip("-")[:60]
    path = f"page-{slug}.md"
    open(path, "w", encoding="utf-8").write(out)
    print("Page rédigée :", path)


# ───────────────────────────── CLI ─────────────────────────────

def slug_of(url):
    return re.sub(r"[^a-z0-9]+", "-", urlparse(ensure_url(url)).netloc.lower()).strip("-")


def main():
    p = argparse.ArgumentParser(description="GEO Agent — visibilité dans les réponses des IA")
    sub = p.add_subparsers(dest="cmd", required=True)
    for c in ("scan", "analyse", "pages", "llms", "jsonld"):
        sp = sub.add_parser(c)
        sp.add_argument("url")
        sp.add_argument("--json", action="store_true", help="sortie JSON (scan)")
    sub.add_parser("track").add_argument("config")
    sub.add_parser("rapport").add_argument("config")
    sp = sub.add_parser("rediger")
    sp.add_argument("config")
    sp.add_argument("question")
    a = p.parse_args()

    if a.cmd in ("track", "rapport", "rediger"):
        {"track": lambda: track(a.config), "rapport": lambda: report(a.config),
         "rediger": lambda: rediger(a.config, a.question)}[a.cmd]()
        return

    try:
        s = Scan(a.url).run()
    except ScanError as ex:
        raise SystemExit(str(ex))
    base = "audit-" + slug_of(a.url)
    if a.cmd == "scan":
        if a.json:
            print(json.dumps(s.as_dict(), ensure_ascii=False, indent=2))
            return
        print_scan(s)
        open(base + ".html", "w", encoding="utf-8").write(scan_html(s))
        print(f"\nRapport : {base}.html")
    elif a.cmd == "analyse":
        print_scan(s)
        print("\nAnalyse par l'IA…")
        ai = agent_analyse(s)
        if ai is None:
            print("(Aucune clé d'IA configurée : rapport sans analyse rédigée.)")
        else:
            print("\n" + ai)
        pages = pages_test(s, 20)
        open(base + ".html", "w", encoding="utf-8").write(scan_html(s, ai, pages))
        open(base + "-llms.txt", "w", encoding="utf-8").write(gen_llms(s))
        open(base + "-jsonld.html", "w", encoding="utf-8").write(gen_jsonld(s))
        print(f"\nFichiers : {base}.html, {base}-llms.txt, {base}-jsonld.html")
    elif a.cmd == "pages":
        for u, st, wc, h in pages_test(s, 20):
            print(f"  {'✓' if wc >= 300 else '✗'} {wc:>5} mots  {st}  {u}  ({h})")
    elif a.cmd == "llms":
        print(gen_llms(s))
    elif a.cmd == "jsonld":
        print(gen_jsonld(s))


if __name__ == "__main__":
    main()
