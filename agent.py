"""Agent de box de la ferme de Claude (POC).

Un sandbox = un utilisateur Unix `sandbox-<slug>` dont le HOME porte la session Claude Code
de la personne. Chaque commande dans un sandbox tourne dans une unité systemd éphémère
bridée (mémoire, CPU, système en lecture seule, pas d'accès à localhost).

REST, jeton Bearer, écoute sur FERME_HOST (jamais une interface publique) :
  GET    /                              page de test
  GET    /api/sandboxes/{slug}               état de connexion (claude auth status)
  PUT    /api/sandboxes/{slug}               crée le sandbox
  DELETE /api/sandboxes/{slug}               déconnecte et détruit le sandbox
  POST   /api/sandboxes/{slug}/login         démarre la connexion → {url}
  PUT    /api/sandboxes/{slug}/login/code    transmet le code → état
  POST   /api/sandboxes/{slug}/runs          lance `claude -p`, un à la fois → résumé + événements,
                                        ou le flux NDJSON (`Accept: application/x-ndjson`)
"""
import json
import os
import pwd
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RACINE = Path("/srv/sandboxes")
PAGE = Path(__file__).with_name("index.html")
JETON = os.environ["FERME_TOKEN"]
HOTE = os.environ["FERME_HOST"]
PORT = int(os.environ.get("FERME_PORT", "8190"))
CLAUDE = "/usr/bin/claude"
# Le Python d'oto-runner, lisible depuis les sandboxes : il lance le relais MCP (`oto_runner.relais_mcp`).
RELAIS_PYTHON = os.environ["FERME_RELAIS_PYTHON"]
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
    "InaccessiblePaths=-/opt -/etc/ferme -/etc/oto-runner -/var/lib/postgresql",
    "RuntimeMaxSec=1800",   # un run qui ne rend jamais la main ne tient pas le sandbox indéfiniment
]

ENVOYE = object()  # la réponse est déjà partie (flux)
connexions = {}  # slug -> Popen de `claude auth login` en attente du code
verrous = {}     # slug -> Lock : un run à la fois par sandbox
verrou_global = threading.Lock()


class Erreur(Exception):
    def __init__(self, statut, message):
        super().__init__(message)
        self.statut = statut


def utilisateur(slug):
    return f"sandbox-{slug}"


def home(slug):
    return RACINE / slug


def exige_sandbox(slug):
    if not SLUG.match(slug):
        raise Erreur(400, "slug invalide")
    try:
        pwd.getpwnam(utilisateur(slug))
    except KeyError:
        raise Erreur(404, "sandbox inconnu")


def unite_de_connexion(slug):
    return f"sandbox-login-{slug}.service"


def unite_de_run(slug):
    return f"sandbox-run-{slug}.service"


def dans_le_sandbox(slug, args, unite=None):
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
    p = dans_le_sandbox(slug, ["auth", "status"])
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
    RACINE.chmod(0o711)  # traverser jusqu'à son sandbox, jamais lister ceux des autres
    subprocess.run(["useradd", "--system", "--create-home", "--home-dir", str(home(slug)),
                    "--shell", "/usr/sbin/nologin", utilisateur(slug)], check=True)
    os.chmod(home(slug), 0o700)
    return {"slug": slug, "cree": True}


def detruire(slug):
    exige_sandbox(slug)
    abandonner_connexion(slug)
    deconnexion = dans_le_sandbox(slug, ["auth", "logout"])
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
    exige_sandbox(slug)
    abandonner_connexion(slug)
    args = ["auth", "login", "--claudeai"] + (["--email", email] if email else [])
    p = dans_le_sandbox(slug, args, unite=unite_de_connexion(slug))
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
    exige_sandbox(slug)
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


def reserver(slug):
    """Un run à la fois par sandbox : deux rafraîchissements simultanés du jeton OAuth,
    chacun faisant tourner le refresh token, casseraient la session."""
    exige_sandbox(slug)
    with verrou_global:
        verrou = verrous.setdefault(slug, threading.Lock())
    if not verrou.acquire(blocking=False):
        raise Erreur(409, "un run tourne déjà dans ce sandbox")
    return verrou


def ecrire_config_mcp(slug, mcp):
    """La config MCP du run : le relais d'oto-runner, lancé DANS le sandbox par le CLI.

    Elle porte le jeton délégué du travail (borné à son bail) : fichier du sandbox, 0600,
    effacé à la fin du run."""
    config = {"mcpServers": {"oto": {
        "type": "stdio", "command": RELAIS_PYTHON, "args": ["-m", "oto_runner.relais_mcp"],
        "env": {"OTO_MCP_URL": mcp["url"], "OTO_TOKEN": mcp["token"],
                "OTO_ORG": str(mcp.get("org") or ""), "OTO_PROJECT": str(mcp.get("project") or ""),
                "OTO_RUN_ID": mcp.get("run_id") or "", "OTO_TOOLS": ",".join(mcp.get("tools") or ())}}}}
    chemin = home(slug) / f".ferme-mcp-{uuid.uuid4().hex}.json"
    compte = pwd.getpwnam(utilisateur(slug))
    fd = os.open(chemin, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    os.chown(chemin, compte.pw_uid, compte.pw_gid)
    return chemin


def flux_du_run(slug, verrou, prompt, modele, system=None, mcp=None):
    """Les événements `stream-json` du CLI, un par un, puis `ferme_resume`.

    Avec `mcp` : les seuls outils du run sont ceux du relais (aucun outil intégré,
    aucune config utilisateur). Libère le sandbox quoi qu'il arrive."""
    config, p = None, None
    try:
        debut = time.time()
        args = ["-p", prompt, "--output-format", "stream-json", "--verbose", "--model", modele]
        if system:
            args += ["--system-prompt", system]
        if mcp is not None:
            config = ecrire_config_mcp(slug, mcp)
            args += ["--mcp-config", str(config), "--strict-mcp-config", "--tools", "",
                     "--allowedTools", "mcp__oto", "--permission-mode", "dontAsk"]
        p = dans_le_sandbox(slug, args, unite=unite_de_run(slug))
        resultat, brut = None, ""
        for ligne in p.stdout:
            try:
                ev = json.loads(ligne)
            except ValueError:
                brut = ligne.strip() or brut     # stderr du CLI : gardé pour dire l'échec
                continue
            if ev.get("type") == "result":
                resultat = ev
            yield ev
        p.wait(timeout=30)
        resume = {"type": "ferme_resume", "duree_s": round(time.time() - debut, 1),
                  "code": p.returncode}
        if resultat is None or resultat.get("is_error"):
            # Un échec se QUALIFIE : sandbox déconnecté (→ needs_login) ou autre chose.
            resume["connecte"] = bool(etat(slug).get("loggedIn"))
            resume["erreur"] = (resultat or {}).get("result") or brut or "flux sans résultat"
        yield resume
    finally:
        # Flux abandonné (le worker s'est arrêté, la connexion est tombée) : le CLI
        # tournerait encore dans le sandbox après sa libération, et un second run pourrait
        # y partir sur la même session. On arrête l'UNITÉ — tuer le client
        # systemd-run ne suffit pas.
        if p is not None and p.poll() is None:
            subprocess.run(["systemctl", "stop", unite_de_run(slug)])
            p.wait(timeout=30)
        if config is not None:
            config.unlink(missing_ok=True)
        verrou.release()


def resume_du_run(evenements):
    """Le run entier, résumé pour la page de test."""
    evenements = list(evenements)
    init = next((e for e in evenements if e.get("subtype") == "init"), {})
    resultat = next((e for e in evenements if e.get("type") == "result"), {})
    fin = next((e for e in evenements if e.get("type") == "ferme_resume"), {})
    limites = [e["rate_limit_info"] for e in evenements if e.get("type") == "rate_limit_event"]
    return {
        "duree_s": fin.get("duree_s"),
        "apiKeySource": init.get("apiKeySource"),
        "modele": init.get("model"),
        "resultat": resultat.get("result") or fin.get("erreur"),
        "erreur": bool(resultat.get("is_error")) or "erreur" in fin,
        "connecte": fin.get("connecte", True),
        "forfait": limites[-1] if limites else None,
        "evenements": evenements,
    }


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
        m = re.match(r"^/api/sandboxes/([^/]+)(/login|/login/code|/runs)?$", self.path)
        if not m:
            return self._repondre(404, {"erreur": "route inconnue"})
        slug, sous = m.groups()
        routes = {
            ("GET", None): lambda: etat(exige_sandbox(slug) or slug),
            ("PUT", None): lambda: creer(slug),
            ("DELETE", None): lambda: detruire(slug),
            ("POST", "/login"): lambda: demarrer_connexion(slug, self._corps().get("email")),
            ("PUT", "/login/code"): lambda: transmettre_code(slug, self._corps()["code"]),
            ("POST", "/runs"): lambda: self._run(slug, self._corps()),
        }
        action = routes.get((methode, sous))
        if action is None:
            return self._repondre(405, {"erreur": "méthode non permise"})
        try:
            reponse = action()
            if reponse is not ENVOYE:
                self._repondre(200, reponse)
        except Erreur as e:
            self._repondre(e.statut, {"erreur": str(e)})
        except (KeyError, ValueError) as e:
            self._repondre(400, {"erreur": f"requête invalide : {e}"})

    def _run(self, slug, c):
        verrou = reserver(slug)
        flux = flux_du_run(slug, verrou, c["prompt"], c.get("model", "sonnet"),
                           system=c.get("system"), mcp=c.get("mcp"))
        if "application/x-ndjson" not in (self.headers.get("Accept") or ""):
            return resume_du_run(flux)
        # Le flux, au fil de l'eau : le worker prolonge son bail à chaque événement.
        # HTTP/1.0 sans longueur : la fin du flux est la fermeture de la connexion.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for ev in flux:
            self.wfile.write(json.dumps(ev, ensure_ascii=False).encode() + b"\n")
            self.wfile.flush()
        return ENVOYE

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
