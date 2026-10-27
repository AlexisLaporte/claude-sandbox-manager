"""Agent de box de la ferme de Claude (POC).

Un sandbox = un utilisateur Unix `sandbox-<slug>` dont le HOME porte ce qui authentifie
Claude Code : la session claude.ai de la personne (`claude auth login`), et/ou une clé
d'API (`/key`). Chaque commande dans un sandbox tourne dans une unité systemd éphémère
bridée (mémoire, CPU, système en lecture seule, pas d'accès à localhost).

Deux façons de payer un run, et c'est la ferme qui tranche, pas le CLI : `auth=key` sert
la clé (posée dans l'environnement de l'unité par `EnvironmentFile=`, jamais en argv),
`auth=session` la session. Sans `auth` : la session, comme avant la clé — poser une clé ne
fait jamais basculer les runs d'un appelant qui ne la demande pas. Et le CLI doit le
confirmer : un run dont `init.apiKeySource` ne dit pas ce qu'on a servi est arrêté.

REST, jeton Bearer, écoute sur FERME_HOST (jamais une interface publique) :
  GET    /                              page de test
  GET    /api/sandboxes/{slug}               état : session (claude auth status) + `apiKey`
  PUT    /api/sandboxes/{slug}               crée le sandbox
  DELETE /api/sandboxes/{slug}               déconnecte et détruit le sandbox
  POST   /api/sandboxes/{slug}/login         démarre la connexion → {url}
  PUT    /api/sandboxes/{slug}/login/code    transmet le code → état
  PUT    /api/sandboxes/{slug}/key           pose une clé d'API {api_key} → état
  DELETE /api/sandboxes/{slug}/key           retire la clé → état
  POST   /api/sandboxes/{slug}/runs          lance `claude -p`, un à la fois → résumé + événements,
                                        ou le flux NDJSON (`Accept: application/x-ndjson`)
"""
import json
import os
import pwd
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RACINE = Path(os.environ.get("FERME_RACINE", "/srv/sandboxes"))
# Les clés d'API vivent HORS des sandboxes, dans un répertoire de root que les brides
# rendent invisible (`/etc/ferme`) : systemd lit l'EnvironmentFile en root et suit les
# liens — un fichier posé dans le HOME du sandbox pourrait y être remplacé par un lien
# vers un secret de la box, versé ensuite dans l'environnement du run.
CLES = Path(os.environ.get("FERME_CLES", "/etc/ferme/cles"))
# La clé remise AVEC un run (celle de l'org, par le worker) ne vit que le temps du run :
# en mémoire (`/run`, tmpfs), jamais sur le disque persistant. Même règle que `CLES` :
# root 0700, rendu invisible aux runs par les brides.
CLES_RUN = Path(os.environ.get("FERME_CLES_RUN", "/run/ferme/cles-run"))
PAGE = Path(__file__).with_name("index.html")
JETON = os.environ["FERME_TOKEN"]
HOTE = os.environ["FERME_HOST"]
PORT = int(os.environ.get("FERME_PORT", "8190"))
CLAUDE = "/usr/bin/claude"
# Le Python d'oto-runner, lisible depuis les sandboxes : il lance le relais MCP (`oto_runner.relais_mcp`).
RELAIS_PYTHON = os.environ["FERME_RELAIS_PYTHON"]
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
URL_LOGIN = re.compile(r"https://claude\.com/cai/oauth/authorize\S+")
# Une clé d'API de la Console, et rien d'autre : la forme est aussi ce qui garantit que la
# ligne écrite dans l'EnvironmentFile ne peut pas en porter une seconde. Trois préfixes : `api`
# (clé historique), `usr` (clé personnelle ou d'espace de travail, la forme des clés créées dans
# la Console depuis 2026) et `svc` (compte de service). Tous trois passent en `x-api-key`.
CLE_API = re.compile(r"^sk-ant-(?:api|usr|svc)[A-Za-z0-9_-]{20,300}$")
# Le jeton d'une session claude.ai (`sk-ant-oat…`) n'est PAS une clé : le poser ici serait
# intermédier un identifiant claude.ai, ce qu'Anthropic interdit. Une session se connecte
# par `/login`, par la personne elle-même.
JETON_SESSION = "sk-ant-oat"
AUTHS = ("key", "session")
# Ce que `system/init` doit annoncer pour chaque mode. Le HOME du sandbox est à lui : un
# `apiKeyHelper` dans ses réglages ferait payer autre chose que ce qui a été demandé.
SOURCE_ATTENDUE = {"key": "ANTHROPIC_API_KEY", "session": "none"}

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
    # `/run/dbus` et les sockets de systemd : par D-Bus, tout utilisateur de la box liste
    # les unités et lit leur `ExecStart` et leur description — celles des runs des AUTRES
    # orgs comprises. Un sandbox ne parle jamais au gestionnaire. `/run/systemd/resolve`
    # reste : `/etc/resolv.conf` y pointe.
    f"InaccessiblePaths=-/opt -/etc/ferme -{CLES_RUN.parent} -/etc/oto-runner -/var/lib/postgresql "
    "-/run/dbus -/run/systemd/private -/run/systemd/io.systemd.Manager",
    # Aucun texte d'un run ne passe plus en argv (`dans_le_sandbox`) ; /proc reste caché
    # entre sandboxes quand même.
    "ProtectProc=invisible", "ProcSubset=pid",
    # Un run ne parle qu'en IP (Anthropic, le relais d'oto, le résolveur sur 127.0.0.53) :
    # ni socket UNIX vers un service de la box, ni netlink. Les tubes et `socketpair`
    # (stdio du relais MCP, sous-processus de node) ne sont pas concernés. Éprouvé avec
    # le CLI 2.1.281 : banc de lecture complet, DNS et HTTPS, `auth login` (21/10/2026).
    "RestrictAddressFamilies=AF_INET AF_INET6",
    "RuntimeMaxSec=1800",   # un run qui ne rend jamais la main ne tient pas le sandbox indéfiniment
]
# La durée qu'un run peut DEMANDER (`max_seconds`, déclarée sur l'agent) : de 60 s à une
# heure, la borne d'oto-backend. Sans demande, les 1800 s des brides. Au-delà d'une
# heure, c'est une question de capacité de la box, pas un réglage.
DUREE_RUN_MIN_S, DUREE_RUN_MAX_S = 60, 3600
# Les bornes d'un run en TOURS et en DOLLARS (`--max-turns`, `--max-budget-usd`) : la ferme
# n'en pose AUCUNE d'office. Elles appartiennent à qui déclare l'agent (`max_turns`,
# `max_budget_usd`, comme `max_seconds`) : déclarées, elles passent au CLI telles quelles ;
# absentes, le run n'est arrêté que par sa durée. La ferme refuse seulement une demande
# qui n'a pas de sens (pas un nombre, zéro ou moins).

# Les sous-agents tournent sur le modèle du run, quoi que le modèle demande : sans ceci,
# le paramètre `model` de `Task` (ou le modèle déclaré d'un agent intégré) choisit un
# modèle plus cher que celui que l'agent a déclaré. `_FORCE` retire ce paramètre (relevé
# sur le CLI 2.1.281) ; `inherit` = le modèle du parent.
MODELE_DES_SOUS_AGENTS = ("CLAUDE_CODE_SUBAGENT_MODEL=inherit", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1")
# Les runs EN VOL sur toute la box, tous sandboxes confondus : au-delà, 429. Les brides
# donnent 500 Mo à chaque run ; la box en tient ce nombre-là, pas un de plus.
RUNS_MAX = int(os.environ.get("FERME_RUNS_MAX", "3"))
# La FILE D'ATTENTE des places (`POST /api/places`) : un worker attend son tour ICI, dans
# l'ordre d'arrivée, AVANT de prendre un travail, au lieu de prendre un travail puis
# d'essuyer des 429. Il attend au plus `ATTENTE_MAX_S` par demande (puis redemande) ; la
# place obtenue est à lui `PLACE_TENUE_S` : le temps de réserver son travail et de poster
# le run qui la consomme (`place` dans le corps). Non consommée à temps (worker mort, aucun
# travail à prendre sans `DELETE`), elle revient à la box.
ATTENTE_MAX_S, PLACE_TENUE_S = 25, 30
# Le workspace d'une clé d'ORGANISATION Anthropic : sans lui, toute requête faite avec
# cette clé est refusée. Il part en en-tête (`ANTHROPIC_CUSTOM_HEADERS`) — une ligne, donc
# une forme fermée, pour la même raison que la clé.
WORKSPACE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
# L'effort que le CLI sait recevoir (`--effort`). `none` est une valeur du catalogue
# d'oto (un modèle qui ne raisonne pas) : elle ne part pas.
EFFORT = re.compile(r"^[a-z]{2,12}$")

#: Les seuls outils INTÉGRÉS d'un run à relais : la délégation à un sous-agent. Ni shell,
#: ni fichiers, ni web. Un sous-agent tourne dans le MÊME processus CLI, donc la même unité
#: bridée, la même clé et le même relais `mcp__oto` ; il hérite du mode `dontAsk` et de la
#: liste d'autorisation du parent. ⚠️ Que ses outils servis restent bornés à ceux du parent
#: (un agent intégré déclare Bash, WebFetch…) s'éprouve sur la box : un sous-agent qui les
#: tente doit être refusé. Le CLI épinglé (2.1.281) sert cet outil sous le nom `Task` (il
#: accepte aussi `Agent`, et le sert quand même en `Task`).
OUTILS_INTEGRES = ("Task", "Read")

#: Au-delà de ce nombre de jetons, le CLI range un résultat d'outil dans un FICHIER
#: texte, une ligne du résultat par ligne du fichier, et `Read` le lit par morceaux
#: (`offset`/`limit`). Sans ce réglage (25 000 par défaut), un résultat entre ~50 Ko et
#: 25 000 jetons part dans un autre format : les blocs MCP en JSON, tout le texte sur
#: UNE ligne échappée, que `Read` refuse en entier (mesuré sur le CLI 2.1.281 : une
#: ligne de 38 851 jetons, refusée même avec `offset`/`limit`). Mesuré sur des profils
#: réels accentués (FR/ES/IT) : à 12 000, 47 k caractères restent en ligne et le fichier
#: texte prend le relais à 49 k — marge trop mince pour un contenu compressible (URL,
#: identifiants). 10 000 range en fichier texte dès ~44 k caractères. Reste illisible :
#: un contenu de plus de ~50 Ko sous 10 000 jetons (> 5 caractères par jeton).
MCP_JETONS_MAX = 10000

#: Ajouté au prompt système d'un run à relais : le message du CLI propose `jq`, que ce
#: run n'a pas — un tour perdu à chaque gros résultat sans cette phrase.
CONSIGNE_LECTURE = (
    "Un résultat d'outil trop gros est rangé dans un fichier dont le message donne le "
    "chemin : lis-le par morceaux avec Read (offset/limit). Il n'y a ni shell, ni jq, "
    "ni Python dans ce run.")

# Les dossiers que l'agent crée, en root, dans le HOME du sandbox : les configs des runs
# par clé, les répertoires de travail des runs à relais, et les textes d'un run qui ne
# passent ni par l'argv ni par stdin (`ecrire_consignes`).
RUNS, TRAVAIL, CONSIGNES = ".ferme-runs", ".ferme-travail", ".ferme-consignes"

#: Un élément de l'argv du CLI : un drapeau ou un identifiant, jamais un texte. L'argv
#: d'une unité se lit par D-Bus (`systemctl show`), dans /proc et dans le journal ; une
#: consigne, un prompt système ou un texte de run y serait lisible par toute la box.
ELEMENT_D_ARGV = re.compile(r"\S+")

ENVOYE = object()  # la réponse est déjà partie (flux)
connexions = {}  # slug -> Popen de `claude auth login` en attente du code
verrous = {}     # slug -> Lock : un run de SESSION à la fois par sandbox
verrou_global = threading.Lock()
places = threading.BoundedSemaphore(RUNS_MAX)   # runs en vol sur la box
reservations = {}  # jeton -> échéance (monotone) : places tenues, pas encore consommées
verrou_reservations = threading.Lock()


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


def fichier_cle(slug):
    return CLES / slug


def cle_presente(slug):
    return fichier_cle(slug).is_file()


def duree_du_run(demande):
    """`RuntimeMaxSec` de l'unité du run : la durée demandée, bornée — ou None (brides)."""
    if demande is None:
        return None
    if isinstance(demande, bool) or not isinstance(demande, int):
        raise Erreur(400, f"max_seconds invalide : {demande!r} (un entier de secondes)")
    return max(DUREE_RUN_MIN_S, min(demande, DUREE_RUN_MAX_S))


def tours_du_run(demande):
    """`--max-turns` du run : les tours demandés — ou aucun (None), sans défaut."""
    if demande is None:
        return None
    if isinstance(demande, bool) or not isinstance(demande, int) or demande < 1:
        raise Erreur(400, f"max_turns invalide : {demande!r} (un entier de tours, au moins 1)")
    return demande


def budget_du_run(demande):
    """`--max-budget-usd` du run : le budget demandé — ou aucun (None), sans défaut."""
    if demande is None:
        return None
    if (isinstance(demande, bool) or not isinstance(demande, (int, float)) or demande != demande
            or demande <= 0):
        raise Erreur(400, f"max_budget_usd invalide : {demande!r} (un montant en dollars, positif)")
    return float(demande)


def description(slug):
    """La description de l'unité : des identifiants. Sans elle, systemd prend la ligne de
    commande — et l'écrit dans `list-units` et le journal."""
    return f"ferme:{slug}"


def dans_le_sandbox(slug, args, unite=None, env=None, duree=None, config=None,
                    repertoire=None, variables=(), entree=None):
    """Commande exécutée comme la personne, dans une unité éphémère bridée.

    `args` : des drapeaux et des identifiants seulement (`ELEMENT_D_ARGV`), refusés sinon.
    Un texte du run passe par `entree` (écrite sur stdin, puis fermée) ou par un fichier
    (`ecrire_consignes`).

    `env` : un fichier de `CLES` (la clé d'API, le workspace) qui entre dans
    l'environnement de l'unité par `EnvironmentFile=` — lu par systemd, jamais dans
    l'argv de `systemd-run`, que tout utilisateur de la box peut lire dans /proc.
    `config` : un `CLAUDE_CONFIG_DIR` propre au run (runs par clé, cf. `Run`).
    `repertoire` : le répertoire de travail (le HOME par défaut). `variables` : des
    `NOM=valeur` de plus, en argv — donc jamais un secret."""
    texte = next((a for a in args if not ELEMENT_D_ARGV.fullmatch(a)), None)
    if texte is not None:
        raise Erreur(500, "un texte en argv est refusé : il passe par stdin ou par un "
                          "fichier (consigne, prompt système)")
    cmd = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect",
           *([f"--unit={unite}"] if unite else []), f"--description={description(slug)}",
           f"--uid={utilisateur(slug)}", f"--gid={utilisateur(slug)}",
           f"--working-directory={repertoire or home(slug)}",
           f"-p", f"ReadWritePaths={home(slug)}",
           *(["-p", f"EnvironmentFile={env}"] if env else []),
           *(["-E", f"CLAUDE_CONFIG_DIR={config}"] if config else []),
           "-E", f"HOME={home(slug)}", "-E", "DISABLE_AUTOUPDATER=1",
           # Un sous-agent tourne en arrière-plan : sans ceci, `claude -p` l'abandonne
           # après 600 s, le travail payé et inachevé (deux runs du 07/09/2026). `0` =
           # aucun plafond (CLI 2.1.281) : la durée de l'unité (`RuntimeMaxSec`, ou
           # `max_seconds`) reste la seule borne de temps.
           "-E", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0",
           "-E", "PATH=/usr/bin:/bin"]
    for v in variables:
        cmd += ["-E", v]
    for b in BRIDES:
        cmd += ["-p", b]
    if duree is not None:
        # APRÈS les brides : pour une même propriété, la dernière affectation gagne.
        cmd += ["-p", f"RuntimeMaxSec={duree}"]
    p = subprocess.Popen(cmd + [CLAUDE, *args], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if entree is not None:
        p.stdin.write(entree)
        p.stdin.close()
    return p


def unite_conforme(unite, slug, args):
    """L'unité lancée ne porte que ce que l'agent a passé : la commande attendue (des
    identifiants, `dans_le_sandbox`) et sa description. Lu après le lancement, par
    `systemctl show` — ce que n'importe quel utilisateur de la box en verrait."""
    vu = subprocess.run(["systemctl", "show", unite, "-p", "ExecStart", "-p", "Description"],
                        capture_output=True, text=True, timeout=10).stdout
    return (f"argv[]={' '.join([CLAUDE, *args])} ;" in vu
            and f"Description={description(slug)}" in vu.splitlines())


def etat(slug):
    """La session, telle que le CLI la voit (sans la clé : `auth status` dirait la clé
    et tairait la session) — et `apiKey`, le fait qu'une clé est posée."""
    p = dans_le_sandbox(slug, ["auth", "status"])
    sortie, _ = p.communicate(timeout=60)
    try:
        session = json.loads(sortie[sortie.index("{"):])
    except ValueError:
        raise Erreur(502, f"auth status illisible : {sortie[-300:]}")
    session["apiKey"] = cle_presente(slug)
    return session


def ecrire_cle(slug, cle):
    """Pose la clé d'API du sandbox : `CLES/<slug>`, root 0600, au format EnvironmentFile.
    Le sandbox ne voit jamais ce fichier ; la clé n'entre que dans l'environnement du run.

    Remplace une clé déjà posée. Ne touche pas à la session : les deux coexistent, et
    c'est `auth` du run qui choisit."""
    exige_sandbox(slug)
    ecrire_env(fichier_cle(slug), cle)
    return etat(slug)


def exige_texte(cle):
    """Une clé est une chaîne : liste, nombre, null ou objet se refusent (422) sans être
    répétés — ni lus comme « pas de clé », ce qui servirait en silence la clé posée."""
    if not isinstance(cle, str):
        raise Erreur(422, "clé d'API invalide : une chaîne est attendue")


def ecrire_env(chemin, cle, workspace=None):
    """Un fichier d'environnement de `CLES` : la clé d'API (et le workspace), root 0600.

    Posé à côté puis renommé : un run en vol lit l'ancien ou le nouveau, jamais un
    fichier tronqué. La FORME de chaque valeur est vérifiée — c'est aussi ce qui garantit
    qu'une valeur ne peut pas porter une seconde ligne."""
    exige_texte(cle)
    cle = cle.strip()
    if cle.startswith(JETON_SESSION):
        raise Erreur(422, "ceci est le jeton d'une session claude.ai, pas une clé d'API — "
                          "une session se connecte par /login")
    if not CLE_API.fullmatch(cle):
        raise Erreur(422, "clé d'API invalide : une clé de la Console Anthropic "
                          "(sk-ant-api…, sk-ant-usr… ou sk-ant-svc…) est attendue")
    if workspace is not None and not WORKSPACE.fullmatch(workspace):
        raise Erreur(422, "workspace invalide")
    chemin.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    provisoire = chemin.with_name(f".{chemin.name}.{uuid.uuid4().hex}")
    fd = os.open(provisoire, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"ANTHROPIC_API_KEY={cle}\n")
        if workspace:
            f.write(f"ANTHROPIC_CUSTOM_HEADERS=anthropic-workspace-id: {workspace}\n")
    os.replace(provisoire, chemin)


def retirer_cle(slug):
    exige_sandbox(slug)
    fichier_cle(slug).unlink(missing_ok=True)
    return etat(slug)


def mode_auth(slug, demande, cle_du_run=None):
    """Ce que le run paiera. Tranché AVANT le flux : une fois les en-têtes partis, un
    refus n'aurait plus de statut HTTP pour se dire. `cle_du_run` : une clé remise avec
    CE run (elle ne vaut que pour lui) — elle exige `auth: "key"`, dite."""
    exige_sandbox(slug)
    if cle_du_run is not None:   # absente = pas de clé du run ; présente = une chaîne
        exige_texte(cle_du_run)
    if demande is None:
        if cle_du_run:
            raise Erreur(400, "une clé remise avec le run exige `auth: \"key\"`")
        return "session"
    if demande not in AUTHS:
        raise Erreur(400, f"auth inconnu : {demande!r} (key ou session)")
    if demande == "session" and cle_du_run:
        raise Erreur(400, "une clé remise avec le run exige `auth: \"key\"`")
    if demande == "key" and not cle_du_run and not cle_presente(slug):
        raise Erreur(409, "aucune clé d'API posée dans ce sandbox, ni remise avec le run")
    return demande


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
    fichier_cle(slug).unlink(missing_ok=True)   # hors du HOME : `userdel --remove` ne l'emporterait pas
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


def _ouvrir_dossier(slug, *noms, creer=False):
    """Le dossier `<HOME>/<noms…>`, ouvert composant par composant SANS suivre de lien — ou
    None s'il manque.

    L'agent tourne en root dans un HOME qui appartient à la personne : un lien posé à la
    place de `.ferme-runs`, `.ferme-travail`, `.claude`… ferait créer, `chown` ou effacer
    ailleurs sur la box. Tout ce qui suit passe par le descripteur rendu (`dir_fd`),
    jamais par le chemin : un lien posé APRÈS la vérification n'est pas suivi non plus.
    `creer` : les composants manquants sont créés (0700) et rendus à la personne."""
    chemin = home(slug)
    try:
        fd = os.open(chemin, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if creer:
            raise
        return None
    compte = pwd.getpwnam(utilisateur(slug)) if creer else None
    for nom in noms:
        chemin = chemin / nom
        try:
            if creer:
                try:
                    os.mkdir(nom, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                suivant = os.open(nom, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return None
            except OSError:
                # ELOOP (un lien) ou ENOTDIR (un fichier) : jamais suivi.
                raise Erreur(500, f"{chemin} n'est pas un dossier : refusé (lien symbolique ?)")
        finally:
            os.close(fd)
        if creer:
            try:
                os.fchown(suivant, compte.pw_uid, compte.pw_gid)
            except BaseException:
                os.close(suivant)
                raise
        fd = suivant
    return fd


def creer_dossier_du_run(slug, parent, ident):
    """`<HOME>/<parent>/<ident>`, NEUF, 0700, à la personne — créé à travers le
    descripteur de `parent`, jamais par le chemin. Sert la config d'un run par clé
    (`RUNS`) et le répertoire de travail d'un run à relais (`TRAVAIL`)."""
    fd = _ouvrir_dossier(slug, parent, creer=True)
    try:
        os.mkdir(ident, 0o700, dir_fd=fd)
        compte = pwd.getpwnam(utilisateur(slug))
        os.chown(ident, compte.pw_uid, compte.pw_gid, dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)
    return home(slug) / parent / ident


def creer_config_du_run(slug, ident):
    """Le `CLAUDE_CONFIG_DIR` d'un run par clé : `<HOME>/.ferme-runs/<ident>`."""
    return creer_dossier_du_run(slug, RUNS, ident)


def _effacer_sous(fd, nom):
    """Efface `nom` dans le dossier ouvert `fd`, sans suivre de lien : `/proc/self/fd/<fd>`
    désigne le VRAI dossier ouvert, quoi qu'on ait posé depuis à son chemin, et
    `shutil.rmtree` refuse une racine qui est un lien."""
    chemin = f"/proc/self/fd/{fd}/{nom}"
    if os.path.islink(chemin):
        os.unlink(chemin)       # le lien lui-même, jamais sa cible
    else:
        shutil.rmtree(chemin, ignore_errors=True)


def effacer_dans(slug, noms, garder=lambda nom: True):
    """Efface, dans `<HOME>/<noms…>` ouvert sans suivre de lien, chaque entrée que `garder`
    retient. Un dossier manquant ou remplacé par un lien : rien, jamais suivi."""
    try:
        fd = _ouvrir_dossier(slug, *noms)
    except Erreur:
        return
    if fd is None:
        return
    try:
        for nom in os.listdir(fd):
            if garder(nom):
                _effacer_sous(fd, nom)
    finally:
        os.close(fd)


def effacer_config_du_run(slug, ident):
    effacer_dans(slug, (RUNS,), lambda nom: nom == ident)


def projet_du_cli(repertoire):
    """Le nom du dossier où le CLI range la session d'un répertoire de travail :
    `<config>/projects/<nom>/`. Chaque caractère hors [A-Za-z0-9] devient `-` (relevé
    sur le CLI 2.1.281 : `/a/.b-c` → `-a--b-c`)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(repertoire))


def effacer_le_travail(slug, travail, config=None):
    """Le répertoire du run et ce que le CLI a rangé pour lui (transcript, résultats
    d'outils : des données du client) — jamais rien hors de ces deux dossiers, et par
    descripteur. `config` : le `CLAUDE_CONFIG_DIR` du run, sous le HOME (`.claude` sans)."""
    effacer_dans(slug, (TRAVAIL,), lambda nom: nom == travail.name)
    base = Path(config).relative_to(home(slug)).parts if config else (".claude",)
    effacer_dans(slug, (*base, "projects"), lambda nom: nom == projet_du_cli(travail))


class Run:
    """Ce qu'UN run tient, et ce qu'il rend à sa fin — quoi qu'il arrive.

    - **session** : le verrou du sandbox (deux rafraîchissements du jeton OAuth, chacun
      faisant tourner le refresh token, casseraient la session) et l'unité fixe
      `sandbox-run-<slug>`. Un à la fois, comme avant.
    - **clé** : aucun rafraîchissement, donc aucun verrou. Plusieurs runs par sandbox, chacun
      avec SON unité (`sandbox-run-<slug>-<id>`), SON `CLAUDE_CONFIG_DIR` (deux CLI ne se
      partagent pas un état) et, si la clé est remise avec le run, SON fichier de clé,
      effacé à la fin : aucune copie ne reste sur la box.
    - une PLACE sur la box, quel que soit le mode (`RUNS_MAX`)."""

    def __init__(self, slug, auth, cle_du_run=None, workspace=None, place=None):
        self.slug, self.auth = slug, auth
        self.verrou = self.env = self.config = self.env_du_run = None
        self.libere = False
        self.ident = uuid.uuid4().hex[:12]
        # Une place réservée (`POST /api/places`) devient celle du run ; échue ou inconnue,
        # le run en demande une comme s'il n'en avait pas apporté.
        if not prendre_reservation(place) and not places.acquire(blocking=False):
            raise Erreur(429, f"la ferme tient déjà {RUNS_MAX} runs — réessaie plus tard")
        try:
            if auth == "session":
                self.verrou = reserver(slug)
                self.unite = unite_de_run(slug)
                return
            self.unite = f"sandbox-run-{slug}-{self.ident}.service"
            if cle_du_run:
                self.env_du_run = CLES_RUN / f"{slug}.run-{self.ident}"
                ecrire_env(self.env_du_run, cle_du_run, workspace)
                self.env = self.env_du_run
            else:
                if workspace:
                    raise Erreur(400, "`workspace` accompagne une clé remise avec le run")
                self.env = fichier_cle(slug)
            self.config = creer_config_du_run(slug, self.ident)
        except BaseException:
            self.liberer()
            raise

    def liberer(self):
        """Rend tout — une fois : l'appelant ET le flux l'appellent, le second ne fait rien."""
        if self.libere:
            return
        self.libere = True
        if self.env_du_run is not None:
            self.env_du_run.unlink(missing_ok=True)
        if self.config is not None:
            effacer_config_du_run(self.slug, self.ident)
        if self.verrou is not None:
            self.verrou.release()
            self.verrou = None
        places.release()


def reserver_place(attente):
    """Attend une place, dans l'ordre d'arrivée, au plus `attente` secondes (borné à
    `ATTENTE_MAX_S`). `{place: jeton, tenue_s}` ; `{place: None}` si aucune ne s'est libérée :
    le worker redemande, sans avoir pris de travail."""
    try:
        attente = float(attente if attente is not None else ATTENTE_MAX_S)
    except (TypeError, ValueError):
        raise Erreur(400, f"attente_s invalide : {attente!r}")
    if not 0 <= attente <= ATTENTE_MAX_S:
        raise Erreur(400, f"attente_s hors de 0–{ATTENTE_MAX_S} : {attente}")
    if not places.acquire(timeout=attente):
        return {"place": None}
    jeton = uuid.uuid4().hex
    with verrou_reservations:
        reservations[jeton] = time.monotonic() + PLACE_TENUE_S
    return {"place": jeton, "tenue_s": PLACE_TENUE_S}


def prendre_reservation(jeton):
    """Retire la réservation : vrai si elle tenait encore une place, qui passe à l'appelant.
    Une seule des deux voies (le run, l'échéance) la retire : la place n'est rendue qu'une fois."""
    if not isinstance(jeton, str) or not jeton:
        return False
    with verrou_reservations:
        return reservations.pop(jeton, None) is not None


def rendre_place(jeton):
    """Le worker n'a pas eu de travail à prendre : la place revient à la box tout de suite."""
    rendue = prendre_reservation(jeton)
    if rendue:
        places.release()
    return {"rendue": rendue}


def echoir_reservations(maintenant=None):
    """Les places réservées et jamais consommées reviennent à la box."""
    maintenant = time.monotonic() if maintenant is None else maintenant
    with verrou_reservations:
        echues = [j for j, fin in reservations.items() if fin <= maintenant]
        for j in echues:
            del reservations[j]
    for _ in echues:
        places.release()
    return len(echues)


def surveiller_reservations():
    while True:
        time.sleep(5)
        echoir_reservations()


def reserver(slug):
    """Un run à la fois par sandbox : deux rafraîchissements simultanés du jeton OAuth,
    chacun faisant tourner le refresh token, casseraient la session."""
    exige_sandbox(slug)
    with verrou_global:
        verrou = verrous.setdefault(slug, threading.Lock())
    if not verrou.acquire(blocking=False):
        raise Erreur(409, "un run tourne déjà dans ce sandbox")
    return verrou


def ecrire_consignes(slug, textes):
    """Les textes d'un run qui ne passent pas par stdin (le prompt système, la consigne
    de lecture) : un fichier chacun, 0600 à la personne, dans
    `<HOME>/.ferme-consignes/<id>/`, effacé en fin de run (`effacer_consignes`) et au
    démarrage. Écrits par le descripteur du dossier, sans suivre de lien. Rend
    `(dossier, {nom: chemin})`."""
    ident = uuid.uuid4().hex
    dossier = creer_dossier_du_run(slug, CONSIGNES, ident)
    compte = pwd.getpwnam(utilisateur(slug))
    fd = _ouvrir_dossier(slug, CONSIGNES, ident)
    chemins = {}
    try:
        for nom, texte in textes.items():
            f = os.open(nom, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                        dir_fd=fd)
            with os.fdopen(f, "w") as sortie:
                os.fchown(sortie.fileno(), compte.pw_uid, compte.pw_gid)
                sortie.write(texte)
            chemins[nom] = dossier / nom
    finally:
        os.close(fd)
    return dossier, chemins


def effacer_consignes(slug, dossier):
    effacer_dans(slug, (CONSIGNES,), lambda nom: nom == dossier.name)


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
    # O_EXCL refuse un lien déjà posé à ce nom ; `fchown` agit sur CE fichier, jamais sur
    # ce qu'un lien posé ensuite au même chemin désignerait.
    fd = os.open(chemin, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchown(f.fileno(), compte.pw_uid, compte.pw_gid)
        json.dump(config, f)
    return chemin


def outils_du_run(maison, repertoire, session, config=None):
    """Les options d'outils d'un run à relais, et rien d'autre (testées à part).

    `Read` ne sert qu'à relire un gros résultat d'outil que le CLI a rangé dans un
    fichier. Il est donc borné au dossier des résultats de CETTE session :
    `<config>/projects/<projet du répertoire>/<session>/tool-results/`, où `config` est
    le dossier de config du CLI : `<maison>/.claude` par défaut (session claude.ai), ou
    le `CLAUDE_CONFIG_DIR` propre au run quand il y en a un (runs par clé). Les refus,
    eux, visent toujours la session claude.ai et les fichiers du HOME.

    ⚠️ Le répertoire de travail est lisible SANS règle : c'est pourquoi un run à relais
    tourne dans un répertoire vide qui lui est propre, jamais dans le HOME — où sont la
    session claude.ai de la personne et la config MCP qui porte le jeton du travail.
    Hors de ce répertoire et de la règle ci-dessous, `dontAsk` refuse. Les refus
    explicites sont une seconde ceinture : un refus l'emporte sur une permission."""
    config = Path(config) if config else Path(maison) / ".claude"
    resultats = config / "projects" / projet_du_cli(repertoire) / session / "tool-results"
    return ["--tools", ",".join(OUTILS_INTEGRES),
            "--allowedTools", "mcp__oto", "Task", f"Read(/{resultats}/**)",
            "--disallowedTools",
            f"Read(/{Path(maison) / '.claude' / '.credentials.json'})",
            f"Read(/{Path(maison) / '.claude.json'})",
            f"Read(/{Path(maison)}/.ferme-mcp-*.json)",
            "Read(//proc/**)",
            "--permission-mode", "dontAsk"]


def options_du_relais(maison, repertoire, session, config_mcp, consigne, config=None):
    """Tout ce qu'un run à relais ajoute à `claude -p` — la MÊME fonction sert le run et
    le banc (`tests/banc_read.py`), pour que le banc éprouve les options réelles.
    `consigne` : le fichier qui porte `CONSIGNE_LECTURE` (`ecrire_consignes`)."""
    return ["--mcp-config", str(config_mcp), "--strict-mcp-config",
            "--session-id", session,
            "--append-system-prompt-file", str(consigne),
            *outils_du_run(maison, repertoire, session, config)]


def flux_du_run(slug, run, prompt, modele, system=None, mcp=None, duree=None, effort=None,
                tours=None, budget=None):
    """Les événements `stream-json` du CLI, un par un, puis `ferme_resume`.

    Avec `mcp` : les seuls outils du run sont ceux du relais, plus la délégation à un
    sous-agent (`OUTILS_INTEGRES`) ; aucune config utilisateur. `run` tient ce que le run a réservé (`Run`) ; tout est
    rendu quoi qu'il arrive."""
    auth, unite = run.auth, run.unite
    config_mcp, travail, consignes, p = None, None, None, None
    try:
        debut = time.time()
        # Le prompt passe sur stdin (`entree`), les autres textes par fichier : jamais en argv.
        args = ["-p", "--output-format", "stream-json", "--verbose", "--model", modele,
                *(["--max-turns", str(tours)] if tours is not None else []),
                *(["--max-budget-usd", f"{budget:g}"] if budget is not None else [])]
        textes = {**({"systeme.txt": system} if system else {}),
                  **({"lecture.txt": CONSIGNE_LECTURE} if mcp is not None else {})}
        if textes:
            consignes, fichiers = ecrire_consignes(slug, textes)
        if system:
            args += ["--system-prompt-file", str(fichiers["systeme.txt"])]
        if effort and effort != "none":
            args += ["--effort", effort]
        variables = MODELE_DES_SOUS_AGENTS
        if mcp is not None:
            config_mcp = ecrire_config_mcp(slug, mcp)
            session = str(uuid.uuid4())
            travail = creer_dossier_du_run(slug, TRAVAIL, session)
            # ⚠️ `--tools` borne les outils intégrés SERVIS ; `--allowedTools` dit ce qui
            # s'exécute sans demander. Les deux, pas l'un ou l'autre. `run.config` : la
            # règle de `Read` suit le dossier où CE CLI range ses résultats.
            args += options_du_relais(home(slug), travail, session, config_mcp,
                                      fichiers["lecture.txt"], run.config)
            variables += (f"MAX_MCP_OUTPUT_TOKENS={MCP_JETONS_MAX}",)
        p = dans_le_sandbox(slug, args, unite=unite, env=run.env, duree=duree,
                            config=run.config, repertoire=travail, variables=variables,
                            entree=prompt)
        resultat, brut = None, ""
        for ligne in p.stdout:
            try:
                ev = json.loads(ligne)
            except ValueError:
                brut = ligne.strip() or brut     # stderr du CLI : gardé pour dire l'échec
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                if not unite_conforme(unite, slug, args):
                    subprocess.run(["systemctl", "stop", unite])
                    p.wait(timeout=30)
                    yield {"type": "ferme_resume", "duree_s": round(time.time() - debut, 1),
                           "code": p.returncode, "auth": auth, "connecte": True,
                           "erreur": "argv_non_conforme : l'unité ne porte pas la seule "
                                     "commande attendue — arrêtée avant tout tour"}
                    return
                source = ev.get("apiKeySource")
                if source != SOURCE_ATTENDUE[auth]:
                    subprocess.run(["systemctl", "stop", unite])
                    p.wait(timeout=30)
                    yield {"type": "ferme_resume", "duree_s": round(time.time() - debut, 1),
                           "code": p.returncode, "auth": auth, "connecte": True,
                           "erreur": f"auth_mismatch : run demandé en `{auth}`, le CLI "
                                     f"annonce apiKeySource={source!r} — arrêté avant tout tour"}
                    return
            if ev.get("type") == "result":
                resultat = ev
            yield ev
        p.wait(timeout=30)
        resume = {"type": "ferme_resume", "duree_s": round(time.time() - debut, 1),
                  "code": p.returncode, "auth": auth}
        if resultat is None or resultat.get("is_error"):
            # Un échec se QUALIFIE : ce qui payait le run est-il encore là ? Session
            # tombée (→ needs_login), clé retirée sous le run — ou autre chose. Une clé
            # REFUSÉE par l'API reste « présente » : le refus est dans `erreur`.
            resume["connecte"] = ((run.env_du_run is not None or cle_presente(slug))
                                  if auth == "key" else bool(etat(slug).get("loggedIn")))
            # Une borne atteinte (`error_max_turns`, `error_max_budget_usd`) n'a pas de
            # `result` : elle se dit dans `errors`, qui porte la borne.
            resume["erreur"] = ((resultat or {}).get("result")
                                or "; ".join((resultat or {}).get("errors") or ())
                                or brut or "flux sans résultat")
        yield resume
    finally:
        # Flux abandonné (le worker s'est arrêté, la connexion est tombée) : le CLI
        # tournerait encore dans le sandbox après sa libération, et un second run pourrait
        # y partir sur la même session. On arrête l'UNITÉ — tuer le client
        # systemd-run ne suffit pas.
        if p is not None and p.poll() is None:
            subprocess.run(["systemctl", "stop", unite])
            p.wait(timeout=30)
        if config_mcp is not None:
            config_mcp.unlink(missing_ok=True)
        if consignes is not None:
            effacer_consignes(slug, consignes)
        if travail is not None:
            # AVANT `liberer` : le transcript d'un run par clé vit dans sa config, que
            # `liberer` efface — l'ordre inverse chercherait un dossier déjà parti.
            effacer_le_travail(slug, travail, run.config)
        run.liberer()


def resume_du_run(evenements):
    """Le run entier, résumé pour la page de test."""
    evenements = list(evenements)
    init = next((e for e in evenements if e.get("subtype") == "init"), {})
    resultat = next((e for e in evenements if e.get("type") == "result"), {})
    fin = next((e for e in evenements if e.get("type") == "ferme_resume"), {})
    limites = [e["rate_limit_info"] for e in evenements if e.get("type") == "rate_limit_event"]
    return {
        "duree_s": fin.get("duree_s"),
        "auth": fin.get("auth"),
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
        p = re.match(r"^/api/places(?:/([0-9a-f]{32}))?$", self.path)
        if p:
            return self._places(methode, p.group(1))
        m = re.match(r"^/api/sandboxes/([^/]+)(/login|/login/code|/key|/runs)?$", self.path)
        if not m:
            return self._repondre(404, {"erreur": "route inconnue"})
        slug, sous = m.groups()
        routes = {
            ("GET", None): lambda: etat(exige_sandbox(slug) or slug),
            ("PUT", None): lambda: creer(slug),
            ("DELETE", None): lambda: detruire(slug),
            ("POST", "/login"): lambda: demarrer_connexion(slug, self._corps().get("email")),
            ("PUT", "/login/code"): lambda: transmettre_code(slug, self._corps()["code"]),
            ("PUT", "/key"): lambda: ecrire_cle(slug, self._corps()["api_key"]),
            ("DELETE", "/key"): lambda: retirer_cle(slug),
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

    def _places(self, methode, jeton):
        if methode == "POST" and jeton is None:
            action = lambda: reserver_place(self._corps().get("attente_s"))
        elif methode == "DELETE" and jeton:
            action = lambda: rendre_place(jeton)
        else:
            return self._repondre(405, {"erreur": "méthode non permise"})
        try:
            self._repondre(200, action())
        except Erreur as e:
            self._repondre(e.statut, {"erreur": str(e)})
        except ValueError as e:
            self._repondre(400, {"erreur": f"requête invalide : {e}"})

    def _run(self, slug, c):
        try:
            return self._run_sur_place(slug, c)
        except BaseException:
            # Refusé avant d'avoir pris la place réservée : elle revient à la box tout de
            # suite, sans attendre son échéance. Prise par le run, il n'y a plus rien à rendre.
            rendre_place(c.get("place"))
            raise

    def _run_sur_place(self, slug, c):
        # Tout ce qui se refuse se refuse AVANT de réserver : un refus ne réserve rien.
        # Le prompt compris : lu après `Run(...)`, son absence gardait une place et
        # laissait la clé du run sur le disque (trois requêtes bloquaient la box).
        prompt = c.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise Erreur(400, "prompt manquant ou vide (un texte)")
        auth = mode_auth(slug, c.get("auth"), c.get("api_key"))
        duree = duree_du_run(c.get("max_seconds"))
        tours = tours_du_run(c.get("max_turns"))
        budget = budget_du_run(c.get("max_budget_usd"))
        effort = c.get("effort")
        if effort is not None and not EFFORT.fullmatch(str(effort)):
            raise Erreur(400, f"effort invalide : {effort!r}")
        run = Run(slug, auth, cle_du_run=c.get("api_key"), workspace=c.get("workspace"),
                  place=c.get("place"))
        flux = flux_du_run(slug, run, prompt, c.get("model", "sonnet"),
                           system=c.get("system"), mcp=c.get("mcp"), duree=duree,
                           effort=effort, tours=tours, budget=budget)
        try:
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
        finally:
            # Quoi qu'il arrive — client parti avant le premier octet, écriture qui casse
            # en vol : le flux s'arrête (son `finally` arrête l'unité), et le run rend
            # sa place, sa clé, sa config. Un flux jamais démarré n'a pas de `finally`
            # à lui : sans ceci, la place était perdue et la clé restait sur le disque.
            flux.close()
            run.liberer()

    def do_GET(self):
        self._router("GET")

    def do_PUT(self):
        self._router("PUT")

    def do_POST(self):
        self._router("POST")

    def do_DELETE(self):
        self._router("DELETE")


def nettoyer_restes():
    """Au démarrage : ce qu'un arrêt en plein run a laissé — les clés remises avec un run
    (`CLES/*.run-*`), les configs de run (`<sandbox>/.ferme-runs/*`), les répertoires de
    travail des runs à relais (`<sandbox>/.ferme-travail/*`), les textes des runs
    (`<sandbox>/.ferme-consignes/*`) et ce que le CLI y a rangé
    (`<sandbox>/.claude/projects/<projet d'un répertoire de travail>` : transcripts et
    résultats d'outils, des données du client). Un redémarrage saute les `finally` des
    fils du serveur : sans ce ménage, chaque déploiement en pleine charge laisserait une
    copie de clé d'org et des résultats sur la box. Tout par descripteur, sans suivre de lien."""
    for d in (CLES, CLES_RUN):      # `CLES` : les restes d'avant le passage en mémoire
        if d.is_dir():
            for f in d.iterdir():
                if ".run-" in f.name:
                    f.unlink(missing_ok=True)
    if RACINE.is_dir():
        for sandbox in RACINE.iterdir():
            slug = sandbox.name
            if sandbox.is_symlink() or not sandbox.is_dir():
                continue
            effacer_dans(slug, (RUNS,))
            effacer_dans(slug, (TRAVAIL,))
            effacer_dans(slug, (CONSIGNES,))
            prefixe = projet_du_cli(home(slug) / TRAVAIL) + "-"
            effacer_dans(slug, (".claude", "projects"), lambda nom: nom.startswith(prefixe))


if __name__ == "__main__":
    nettoyer_restes()
    threading.Thread(target=surveiller_reservations, daemon=True).start()
    ThreadingHTTPServer((HOTE, PORT), Handler).serve_forever()
