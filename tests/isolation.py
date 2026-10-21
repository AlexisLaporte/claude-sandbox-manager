"""Lance isolation.js dans un sandbox avec exactement les brides de l'agent."""
import os, subprocess, sys, time
os.environ.setdefault("FERME_TOKEN", "x")
os.environ.setdefault("FERME_HOST", "127.0.0.1")  # seules les brides servent ici
os.environ.setdefault("FERME_RELAIS_PYTHON", "/usr/bin/python3")
sys.path.insert(0, "/opt/ferme")
import agent
slug = sys.argv[1]
cmd = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect",
       f"--uid=sandbox-{slug}", f"--gid=sandbox-{slug}", "-p", f"ReadWritePaths={agent.home(slug)}",
       "-E", f"HOME={agent.home(slug)}"]
for b in agent.BRIDES:
    cmd += ["-p", b]
subprocess.run(cmd + ["/usr/bin/node", "/usr/local/share/ferme/isolation.js"], timeout=60)

# L'unité d'un run ne porte que des identifiants : un `claude -p` lancé comme un run, qui
# attend son prompt sur stdin (rien n'est envoyé, aucun appel ne part), puis arrêté.
unite = f"sandbox-essai-{slug}"
p = agent.dans_le_sandbox(slug, ["-p", "--output-format", "stream-json", "--verbose"],
                          unite=unite)
try:
    time.sleep(3)
    print("unité conforme" if agent.unite_conforme(
        unite, slug, ["-p", "--output-format", "stream-json", "--verbose"]) else "unité NON CONFORME")
finally:
    subprocess.run(["systemctl", "stop", unite])
    p.wait(timeout=30)
