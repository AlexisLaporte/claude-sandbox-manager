"""Agent de box de la ferme de Claude (POC).

Un bac = un utilisateur Unix `bac-<slug>` dont le HOME porte la session Claude Code
de la personne. Chaque commande dans un bac tourne dans une unité systemd éphémère
bridée (mémoire, CPU, système en lecture seule, pas d'accès à localhost).

REST, jeton Bearer, écoute sur FERME_HOST (jamais une interface publique) :
  GET    /                              page de test
  GET    /api/bacs/{slug}               état de connexion (claude auth status)
  PUT    /api/bacs/{slug}               crée le bac
  DELETE /api/bacs/{slug}               déconnecte et détruit le bac
  POST   /api/bacs/{slug}/login         démarre la connexion → {url}
  PUT    /api/bacs/{slug}/login/code    transmet le code → état
  POST   /api/bacs/{slug}/runs          lance `claude -p`, un à la fois → résumé + événements
"""
import json
import os
import pwd
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RACINE = Path("/srv/ferme/bacs")
PAGE = Path(__file__).with_name("index.html")
JETON = os.environ["FERME_TOKEN"]
HOTE = os.environ["FERME_HOST"]
PORT = int(os.environ.get("FERME_PORT", "8190"))
CLAUDE = "/usr/bin/claude"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
URL_LOGIN = re.compile(r"https://claude\.com/cai/oauth/authorize\S+")

BRIDES = [
    "MemoryMax=500M", "MemorySwapMax=0", "CPUQuota=80%", "TasksMax=64",
    "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes",
    "NoNewPrivileges=yes", "ProtectKernelTunables=yes", "ProtectControlGroups=yes",
    "RestrictSUIDSGID=yes", # Métadonnées Scaleway (169.254.42.42 : user-data, clés SSH, sans authentification),
    # réseaux privés et locaux, IPv4 et IPv6.
    "IPAddressDeny=127.0.0.0/8 ::1/128 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 "
    "100.64.0.0/10 fc00::/7 fe80::/10",
    # Le résolveur est systemd-resolved sur localhost : sans lui, pas de DNS, pas d'Anthropic.
    "IPAddressAllow=127.0.0.53/32 127.0.0.54/32",
    "InaccessiblePaths=-/opt -/etc/ferme -/var/lib/postgresql",
]

connexions = {}  # slug -> Popen de `claude auth login` en attente du code
verrous = {}     # slug -> Lock : un run à la fois par bac
verrou_global = threading.Lock()


class Erreur(Exception):
    def __init__(self, statut, message):
        super().__init__(message)
        self.statut = statut


def utilisateur(slug):
    return f"bac-{slug}"


def home(slug):
    return RACINE / slug


def exige_bac(slug):
    if not SLUG.match(slug):
        raise Erreur(400, "slug invalide")
    try:
        pwd.getpwnam(utilisateur(slug))
    except KeyError:
        raise Erreur(404, "bac inconnu")


def unite_de_connexion(slug):
    return f"ferme-login-{slug}.service"


def dans_le_bac(slug, args, unite=None):
    """Commande exécutée comme la personne, dans une unité éphémère bridée."""
    cmd = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect",
           *([f"--unit={unite}"] if unite else []),
           f"--uid={utilisateur(slug)}", f"--gid={utilisateur(slug)}",
           f"--working-directory={home(slug)}",
           f"-p", f"ReadWritePaths={home(slug)}",
           "-E", f"HOME={home(slug)}", "-E", "DISABLE_AUTOUPDATER=1",
           "-E", "PATH=/usr/bin:/bin"]
    for b in BRIDES:
        cmd += ["-p", b]
    return subprocess.Popen(cmd + [CLAUDE, *args], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def etat(slug):
    p = dans_le_bac(slug, ["auth", "status"])
    sortie, _ = p.communicate(timeout=60)
    try:
        return json.loads(sortie[sortie.index("{"):])
    except ValueError:
        raise Erreur(502, f"auth status illisible : {sortie[-300:]}")


def creer(slug):
    if not SLUG.match(slug):
        raise Erreur(400, "slug invalide")
    try:
        pwd.getpwnam(utilisateur(slug))
        return {"slug": slug, "cree": False}
    except KeyError:
        pass
    RACINE.mkdir(parents=True, exist_ok=True)
    RACINE.chmod(0o711)  # traverser jusqu'à son bac, jamais lister ceux des autres
    subprocess.run(["useradd", "--system", "--create-home", "--home-dir", str(home(slug)),
                    "--shell", "/usr/sbin/nologin", utilisateur(slug)], check=True)
    os.chmod(home(slug), 0o700)
    return {"slug": slug, "cree": True}


def detruire(slug):
    exige_bac(slug)
    abandonner_connexion(slug)
    deconnexion = dans_le_bac(slug, ["auth", "logout"])
    deconnexion.communicate(timeout=60)
    # Un run encore en vol empêcherait userdel : la destruction l'emporte.
    subprocess.run(["pkill", "--signal", "KILL", "--uid", utilisateur(slug)])
    time.sleep(1)
    subprocess.run(["userdel", "--remove", utilisateur(slug)], check=True)
    if home(slug).exists():
        raise Erreur(500, f"{home(slug)} existe encore après userdel")
    return {"slug": slug, "detruit": True, "deconnecte": deconnexion.returncode == 0}


def abandonner_connexion(slug):
    """Tuer le client systemd-run ne suffit pas : l'unité survivrait. On l'arrête."""
    p = connexions.pop(slug, None)
    if p is None:
        return
    subprocess.run(["systemctl", "stop", unite_de_connexion(slug)], check=True)
    p.wait(timeout=10)


def demarrer_connexion(slug, email):
    exige_bac(slug)
    abandonner_connexion(slug)
    args = ["auth", "login", "--claudeai"] + (["--email", email] if email else [])
    p = dans_le_bac(slug, args, unite=unite_de_connexion(slug))
    lu, limite = "", time.time() + 30
    while time.time() < limite:
        ligne = p.stdout.readline()
        if not ligne:
            break
        lu += ligne
        m = URL_LOGIN.search(ligne)
        if m:
            connexions[slug] = p
            return {"url": m.group(0)}
    p.kill()
    raise Erreur(502, f"aucune URL de connexion dans la sortie : {lu[-300:]}")


def transmettre_code(slug, code):
    exige_bac(slug)
    p = connexions.pop(slug, None)
    if p is None or p.poll() is not None:
        raise Erreur(409, "aucune connexion en attente — la redémarrer")
    try:
        sortie, _ = p.communicate(code.strip() + "\n", timeout=60)
    except subprocess.TimeoutExpired:
        p.kill()
        raise Erreur(504, "le CLI n'a pas conclu la connexion")
    if p.returncode != 0:
        raise Erreur(422, f"connexion refusée : {sortie[-300:]}")
    return etat(slug)


def lancer_run(slug, prompt, modele):
    exige_bac(slug)
    with verrou_global:
        verrou = verrous.setdefault(slug, threading.Lock())
    if not verrou.acquire(blocking=False):
        raise Erreur(409, "un run tourne déjà dans ce bac")
    try:
        debut = time.time()
        p = dans_le_bac(slug, ["-p", prompt, "--output-format", "stream-json", "--verbose",
                               "--model", modele])
        sortie, _ = p.communicate(timeout=600)
        evenements = []
        for ligne in sortie.splitlines():
            try:
                evenements.append(json.loads(ligne))
            except ValueError:
                evenements.append({"type": "brut", "texte": ligne})
        init = next((e for e in evenements if e.get("subtype") == "init"), {})
        resultat = next((e for e in evenements if e.get("type") == "result"), None)
        limites = [e["rate_limit_info"] for e in evenements if e.get("type") == "rate_limit_event"]
        if resultat is None:
            raise Erreur(502, f"run sans résultat : {sortie[-500:]}")
        return {
            "duree_s": round(time.time() - debut, 1),
            "apiKeySource": init.get("apiKeySource"),
            "modele": init.get("model"),
            "resultat": resultat.get("result"),
            "erreur": resultat.get("is_error"),
            "forfait": limites[-1] if limites else None,
            "evenements": evenements,
        }
    finally:
        verrou.release()


class Handler(BaseHTTPRequestHandler):
    def _repondre(self, statut, corps, type_="application/json"):
        donnees = corps if isinstance(corps, bytes) else json.dumps(corps, ensure_ascii=False).encode()
        self.send_response(statut)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(donnees)))
        self.end_headers()
        self.wfile.write(donnees)

    def _corps(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _router(self, methode):
        if methode == "GET" and self.path == "/":
            return self._repondre(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if self.headers.get("Authorization") != f"Bearer {JETON}":
            return self._repondre(401, {"erreur": "jeton manquant ou invalide"})
        m = re.match(r"^/api/bacs/([^/]+)(/login|/login/code|/runs)?$", self.path)
        if not m:
            return self._repondre(404, {"erreur": "route inconnue"})
        slug, sous = m.groups()
        routes = {
            ("GET", None): lambda: etat(exige_bac(slug) or slug),
            ("PUT", None): lambda: creer(slug),
            ("DELETE", None): lambda: detruire(slug),
            ("POST", "/login"): lambda: demarrer_connexion(slug, self._corps().get("email")),
            ("PUT", "/login/code"): lambda: transmettre_code(slug, self._corps()["code"]),
            ("POST", "/runs"): lambda: lancer_run(slug, (c := self._corps())["prompt"],
                                                  c.get("model", "sonnet")),
        }
        action = routes.get((methode, sous))
        if action is None:
            return self._repondre(405, {"erreur": "méthode non permise"})
        try:
            self._repondre(200, action())
        except Erreur as e:
            self._repondre(e.statut, {"erreur": str(e)})
        except (KeyError, ValueError) as e:
            self._repondre(400, {"erreur": f"requête invalide : {e}"})

    def do_GET(self):
        self._router("GET")

    def do_PUT(self):
        self._router("PUT")

    def do_POST(self):
        self._router("POST")

    def do_DELETE(self):
        self._router("DELETE")


if __name__ == "__main__":
    ThreadingHTTPServer((HOTE, PORT), Handler).serve_forever()
