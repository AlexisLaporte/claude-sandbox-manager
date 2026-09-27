"""Banc de `Read` borné — le VRAI CLI, les options EXACTES d'`agent.outils_du_run`.

Aucune clé, aucun réseau : une API Anthropic simulée (sur 127.0.0.1) joue le modèle et
demande des lectures ; un faux serveur MCP rend un gros résultat, que le CLI range dans
un fichier. Le banc vérifie ce que le CLI laisse lire, et ce qu'il refuse.

    python3 tests/banc_read.py [chemin du CLI]     # défaut : `claude` du PATH

À lancer SUR LA BOX avant de servir `Read` (Linux : `/proc`, propriétaire des fichiers) —
il a été écrit et passé sur macOS avec le CLI 2.1.281. Code de sortie 0 = tout est conforme.

Ce qu'il prouve, lecture par lecture :
- le gros résultat rangé est LISIBLE par morceaux (ligne 200 d'un fichier de ~300) ;
- la session claude.ai, `~/.claude.json`, la config MCP (qui porte le jeton du travail),
  le résultat d'une AUTRE session, `/proc/self/environ` (la clé d'API) et `/etc/hosts`
  sont REFUSÉS ;
- un fichier du répertoire de travail du run reste lisible (il est vide en production).
"""
from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _v in ("FERME_TOKEN", "FERME_HOST", "FERME_RELAIS_PYTHON"):  # exigées à l'import, inutiles ici
    os.environ.setdefault(_v, "banc")
import agent  # noqa: E402

CLI = sys.argv[1] if len(sys.argv) > 1 else "claude"
MARQUE_PROCEDURE = "PROCEDURE-DU-RUN-7f3a"

FAUX_MCP = r'''
import sys, json
def send(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    m = json.loads(line); mid = m.get("id"); meth = m.get("method")
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": m["params"].get("protocolVersion", "2025-06-18"),
              "capabilities": {"tools": {}}, "serverInfo": {"name": "oto", "version": "0"}}})
    elif meth == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": n, "description": "page", "inputSchema": {"type": "object", "properties": {}}}
            for n in ("big", "longues")]}})
    elif meth == "tools/call":
        if m["params"]["name"] == "longues":  # fiches de ~5 Ko et ~30 Ko (vue `full=true`)
            mots = " ".join("experiencia-%d cliente réseau magasin" % i for i in range(1500))
            rows = [{"id": i, "resume": mots[: (5000 if i % 2 == 0 else 30000)], "fin": "FIN-%d" % i}
                    for i in range(6)]
        else:
            rows = [{"id": i, "title": "Marketing Director %d" % i, "pad": "x" * 100} for i in range(340)]
        text = "{\"total\":%d,\"rows\":[\n" % len(rows) + ",\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n]}"
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
'''


def _sse(blocs, arret):
    ev = [("message_start", {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
           "model": "x", "content": [], "stop_reason": None, "usage": {"input_tokens": 1, "output_tokens": 1}}})]
    for i, b in enumerate(blocs):
        if b["type"] == "text":
            ev += [("content_block_start", {"type": "content_block_start", "index": i,
                    "content_block": {"type": "text", "text": ""}}),
                   ("content_block_delta", {"type": "content_block_delta", "index": i,
                    "delta": {"type": "text_delta", "text": b["text"]}})]
        else:
            ev += [("content_block_start", {"type": "content_block_start", "index": i,
                    "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}),
                   ("content_block_delta", {"type": "content_block_delta", "index": i,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}})]
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    ev += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": arret},
            "usage": {"output_tokens": 5}}), ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in ev).encode()


def banc():
    racine = Path(tempfile.mkdtemp(prefix="banc-read-")).resolve()
    maison = racine / "maison"
    session = str(uuid.uuid4())
    travail = maison / ".ferme-travail" / session
    travail.mkdir(parents=True)
    (maison / ".claude").mkdir()
    (maison / ".claude" / ".credentials.json").write_text('{"secret":"SESSION-CLAUDE"}')
    (maison / ".claude.json").write_text('{"secret":"CLAUDE-JSON"}')
    (maison / ".ferme-mcp-abc.json").write_text('{"secret":"JETON-DU-TRAVAIL"}')
    autre = maison / ".claude" / "projects" / "-autre" / "autre-session" / "tool-results"
    autre.mkdir(parents=True)
    (autre / "x.txt").write_text("RESULTAT-D-UN-AUTRE-RUN")
    (travail / "local.txt").write_text("fichier-du-run")
    (racine / "faux_mcp.py").write_text(FAUX_MCP)
    mcp = racine / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"oto": {"command": sys.executable,
                                                      "args": [str(racine / "faux_mcp.py")]}}}))

    lectures = [  # (étiquette, chemin — `RANGE` = le fichier rangé, `DOSSIER` = son dossier, attendu)
        ("resultat range, ligne 200", "RANGE", {"offset": 200, "limit": 2}, True),
        ("../ vers la session claude.ai", "DOSSIER/../../../../.credentials.json", {}, False),
        ("../ vers /etc/hosts", "DOSSIER" + "/.." * 25 + "/etc/hosts", {}, False),
        ("session claude.ai", str(maison / ".claude" / ".credentials.json"), {}, False),
        ("~/.claude.json", str(maison / ".claude.json"), {}, False),
        ("config MCP (jeton)", str(maison / ".ferme-mcp-abc.json"), {}, False),
        ("resultat d'un autre run", str(autre / "x.txt"), {}, False),
        ("/proc/self/environ", "/proc/self/environ", {}, False),
        ("/etc/hosts", "/etc/hosts", {}, False),
        ("fichier du repertoire de travail", str(travail / "local.txt"), {}, True),
        ("fiche de ~30 Ko, entiere", "LONGUES", {"offset": 3, "limit": 1}, True),
    ]
    etat = {"n": 0, "range": None, "longues": None, "systeme": None}
    recus = {}

    class API(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200); self.end_headers()

        def do_POST(self):
            r = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "count_tokens" in self.path:
                b = b'{"input_tokens":10}'
                self.send_response(200); self.send_header("Content-Length", str(len(b)))
                self.end_headers(); self.wfile.write(b); return
            if etat["systeme"] is None:
                sy = r.get("system")
                etat["systeme"] = sy if isinstance(sy, str) else json.dumps(sy, ensure_ascii=False)
            dernier = r["messages"][-1]
            for c in dernier.get("content") if isinstance(dernier.get("content"), list) else []:
                if c.get("type") == "tool_result":
                    t = c.get("content")
                    t = t if isinstance(t, str) else "".join(x.get("text", "") for x in t or [])
                    recus[c["tool_use_id"]] = (t, bool(c.get("is_error")))
                    m = re.search(r"saved to:? (/\S+?\.(?:txt|json))\b", t)
                    if m and c["tool_use_id"] == "big":
                        etat["range"] = m.group(1)
                    if m and c["tool_use_id"] == "longues":
                        etat["longues"] = m.group(1)
            etat["n"] += 1
            k = etat["n"] - 3  # tours 1 et 2 : les deux outils ; puis une lecture par tour
            if etat["n"] <= 2:
                nom = "big" if etat["n"] == 1 else "longues"
                bloc = [{"type": "tool_use", "id": nom, "name": f"mcp__oto__{nom}", "input": {}}]
            elif k < len(lectures):
                _, chemin, extra, _ = lectures[k]
                rang = etat["range"] or "/introuvable/x"
                if chemin == "RANGE":
                    chemin = rang
                elif chemin == "LONGUES":
                    chemin = etat["longues"] or "/introuvable"
                elif chemin.startswith("DOSSIER"):
                    chemin = str(Path(rang).parent) + chemin[len("DOSSIER"):]
                bloc = [{"type": "tool_use", "id": f"l{k}", "name": "Read", "input": {"file_path": chemin, **extra}}]
            else:
                bloc = [{"type": "text", "text": "fin"}]
            corps = _sse(bloc, "tool_use" if bloc[0]["type"] == "tool_use" else "end_turn")
            self.send_response(200); self.send_header("content-type", "text/event-stream")
            self.send_header("Content-Length", str(len(corps))); self.end_headers(); self.wfile.write(corps)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), API)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {"HOME": str(maison), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{srv.server_address[1]}",
           "ANTHROPIC_API_KEY": "sk-ant-api03-banc", "DISABLE_AUTOUPDATER": "1",
           "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0",
           "MAX_MCP_OUTPUT_TOKENS": str(agent.MCP_JETONS_MAX)}
    # Comme `flux_du_run` : `--system-prompt` (la procédure, que le runner envoie
    # toujours) PUIS les options du relais, dont `--append-system-prompt`.
    argv = [CLI, "-p", "go", "--output-format", "stream-json", "--verbose", "--model", "claude-sonnet-5",
            "--system-prompt", MARQUE_PROCEDURE,
            *agent.options_du_relais(maison, travail, session, mcp)]
    subprocess.run(argv, cwd=travail, env=env, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    srv.shutdown()

    echecs = 0
    systeme = etat["systeme"] or ""
    for etiquette, attendu in (("prompt système : la procédure", MARQUE_PROCEDURE),
                               ("prompt système : la consigne de lecture", agent.CONSIGNE_LECTURE)):
        ok = attendu in systeme
        echecs += not ok
        print(f"{'OK ' if ok else 'KO '} {etiquette}")
    print(f"fichier rangé : {etat['range'] and Path(etat['range']).name}")
    for k, (etiquette, _, _, permis) in enumerate(lectures):
        texte, erreur = recus.get(f"l{k}", ("(aucune réponse)", True))
        lu = not erreur
        fuite = any(s in texte for s in ("SESSION-CLAUDE", "CLAUDE-JSON", "JETON-DU-TRAVAIL",
                                         "RESULTAT-D-UN-AUTRE-RUN", "ANTHROPIC_API_KEY"))
        ok = (lu == permis) and not (fuite and not permis)
        if etiquette.startswith("fiche de ~30"):  # ligne 3 = la fiche id 1 (~30 Ko) : entière = sa fin est là
            ok = ok and '"fin": "FIN-1"' in texte
            texte = f"{len(texte)} caractères rendus, fin {'présente' if ok else 'ABSENTE'}"
        echecs += not ok
        print(f"{'OK ' if ok else 'KO '} {etiquette:34} {'lu' if lu else 'refusé':7} {texte[:70]!r}")
    return echecs


if __name__ == "__main__":
    sys.exit(1 if banc() else 0)
