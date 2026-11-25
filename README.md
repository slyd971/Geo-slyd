# GEO Slyd — version en ligne (Vercel)

**Adresse : https://geo-slyd.presskit.fr** (domaine personnalisé relié au projet Vercel `geo-slyd`). Le site est exclu des moteurs de recherche (robots.txt + noindex).

Une app web adaptée au mobile : audit GEO sur 32 critères, suivi des citations dans ChatGPT, Perplexity, Gemini, Claude et Le Chat, correctifs prêts à coller (llms.txt, JSON-LD), rédaction de pages. L'accès est protégé par un mot de passe.

## Accès

- **https://geo-slyd.presskit.fr** : l'audit (score, plan d'action, llms.txt, JSON-LD, test des pages) est ouvert, sans mot de passe. Le site n'est pas référencé (robots.txt + noindex).
- Les fonctions qui consomment des crédits IA (analyse par l'IA, citations, rédaction) demandent le mot de passe `APP_PASSWORD`, une seule fois par appareil. Pour tout ouvrir sans mot de passe : variable `PUBLIC_AI=1` (n'importe qui avec le lien pourra alors utiliser tes crédits).

## Mise en ligne (10 minutes)

### Option A : par GitHub (recommandé)

1. Crée un dépôt **privé** sur GitHub et mets-y le contenu de ce dossier.
2. Sur vercel.com : **Add New → Project**, puis importe le dépôt. Laisse les réglages par défaut (Framework : Other).
3. Avant de cliquer sur Deploy, ouvre **Environment Variables** et ajoute :

| Variable | Obligatoire | À quoi elle sert |
|---|---|---|
| `APP_PASSWORD` | Oui | Le mot de passe pour ouvrir l'app |
| `ANTHROPIC_API_KEY` | Conseillée | Claude : suivi des citations + analyse + rédaction |
| `OPENAI_API_KEY` | Non | ChatGPT avec recherche web |
| `PERPLEXITY_API_KEY` | Non | Perplexity |
| `GEMINI_API_KEY` | Non | Gemini avec Google Search |
| `MISTRAL_API_KEY` | Non | Le Chat (sans recherche web), analyse 100 % UE |

4. Clique sur **Deploy**. Le projet est servi sur https://geo-slyd.presskit.fr (Settings → Domains).

Pour modifier une clé plus tard : Settings → Environment Variables, puis **Redeploy**, sinon la nouvelle valeur n'est pas prise en compte.

### Option B : depuis le Terminal du Mac

```bash
cd geo-agent-web
npx vercel           # première fois : connexion au compte, puis questions par défaut
npx vercel env add APP_PASSWORD
npx vercel env add ANTHROPIC_API_KEY
npx vercel --prod
```

## Installer sur le téléphone

- **iPhone (Safari)** : ouvre https://geo-slyd.presskit.fr → bouton Partager → **Sur l'écran d'accueil**.
- **Android (Chrome)** : ouvre https://geo-slyd.presskit.fr, puis menu ⋮ → **Ajouter à l'écran d'accueil** (ou Installer l'application).

L'app s'ouvre alors en plein écran, comme une vraie app.

## Version gratuite

- Sans aucune clé : audit 32 critères, llms.txt, JSON-LD, test des pages. Tout fonctionne.
- Avec une clé **Gemini** gratuite (aistudio.google.com/apikey) : suivi des citations dans Gemini, analyse rédigée et rédaction de pages, dans la limite du quota gratuit de Google.
- Option : une clé **Mistral** gratuite (offre Experiment sur console.mistral.ai) ajoute Le Chat et une analyse 100 % UE.

## Bon à savoir

- **Historique des citations** : il est gardé sur le téléphone (dans le navigateur). Si tu changes de téléphone ou vides les données de Safari, il repart à zéro. Pour un suivi automatique chaque semaine sans ouvrir l'app, il faudra ajouter une base de données (Vercel Postgres ou Upstash) et une tâche planifiée.
- **Coût** : chaque relevé fait un appel par question et par IA. 10 questions × 4 IA = 40 appels, soit quelques centimes à quelques dizaines de centimes selon les modèles.
- **Temps limite** : chaque appel est limité à 60 secondes. Un site très lent peut faire échouer l'audit : relance-le.
- **Sécurité** : sans mot de passe, l'API refuse tout. L'outil refuse aussi d'analyser des adresses internes (localhost, réseau privé).
- **Offre Hobby de Vercel** : elle est gratuite mais réservée à un usage non commercial. Si tu utilises l'outil pour des clients, l'offre Pro est la règle.
- **Changer de modèle** : variables `GEO_OPENAI_MODEL`, `GEO_PERPLEXITY_MODEL`, `GEO_GEMINI_MODEL`, `GEO_ANTHROPIC_MODEL`, `GEO_MISTRAL_MODEL`. `GEO_ANALYSIS=mistral` force l'analyse 100 % UE.

## Tester sur ton Mac avant de mettre en ligne

```bash
cd geo-agent-web
python3 -m pip install -r requirements.txt
APP_PASSWORD=test python3 api/index.py
# ouvre http://localhost:8000
```

## Contenu

- `public/index.html` : l'outil (installable sur l'écran d'accueil)
- `public/assets/` : logo PressKit For You et police Manrope
- `api/index.py` : l'API (Flask)
- `api/_geo.py` : le moteur GEO. C'est aussi l'outil en ligne de commande : `python3 api/_geo.py scan presskit.fr`
