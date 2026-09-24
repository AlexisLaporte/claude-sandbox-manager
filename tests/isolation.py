"""Lance isolation.js dans un sandbox avec exactement les brides de l'agent."""
import os, subprocess, sys
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
