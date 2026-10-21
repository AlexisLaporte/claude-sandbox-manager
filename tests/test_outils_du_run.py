"""Les options d'outils d'un run à relais — ce qu'un changement ne doit pas perdre en silence.

Le banc (`tests/banc_read.py`) prouve que ces options tiennent contre le vrai CLI ; ce
fichier fige leur FORME, pour qu'une retouche qui retirerait un refus ou élargirait
`Read` casse ici plutôt qu'en production."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _v in ("FERME_TOKEN", "FERME_HOST", "FERME_RELAIS_PYTHON"):
    os.environ.setdefault(_v, "test")
import agent  # noqa: E402

MAISON = Path("/srv/sandboxes/alice")
SESSION = "11111111-2222-4333-8444-555555555555"
TRAVAIL = MAISON / ".ferme-travail" / SESSION


def _valeurs(args, option):
    """Les valeurs qui suivent `option` jusqu'à l'option suivante."""
    i = args.index(option) + 1
    fin = next((j for j in range(i, len(args)) if args[j].startswith("--")), len(args))
    return args[i:fin]


class OutilsDuRun(unittest.TestCase):
    def setUp(self):
        self.args = agent.outils_du_run(MAISON, TRAVAIL, SESSION)

    def test_projet_du_cli_suit_la_regle_du_cli(self):
        # relevé sur le CLI 2.1.281 : tout caractère hors [A-Za-z0-9] devient `-`
        self.assertEqual(agent.projet_du_cli("/a/.b-c_d"), "-a--b-c-d")

    def test_read_n_est_permis_que_sur_les_resultats_de_cette_session(self):
        permis = _valeurs(self.args, "--allowedTools")
        reads = [p for p in permis if p.startswith("Read")]
        self.assertEqual(reads, [
            "Read(//srv/sandboxes/alice/.claude/projects/"
            f"{agent.projet_du_cli(TRAVAIL)}/{SESSION}/tool-results/**)"])
        self.assertNotIn("Read", permis, "un `Read` nu lirait tout le système de fichiers")

    def test_les_refus_couvrent_la_session_le_jeton_et_proc(self):
        refus = _valeurs(self.args, "--disallowedTools")
        for attendu in ("Read(//srv/sandboxes/alice/.claude/.credentials.json)",
                        "Read(//srv/sandboxes/alice/.claude.json)",
                        "Read(//srv/sandboxes/alice/.ferme-mcp-*.json)",
                        "Read(//proc/**)"):
            self.assertIn(attendu, refus)

    def test_aucun_outil_d_ecriture_ni_shell(self):
        servis = _valeurs(self.args, "--tools")[0].split(",")
        self.assertEqual(sorted(servis), ["Read", "Task"])
        self.assertEqual(_valeurs(self.args, "--permission-mode"), ["dontAsk"])

    def test_avec_un_dossier_de_config_propre_au_run(self):
        config = MAISON / ".ferme-runs" / "7"
        args = agent.outils_du_run(MAISON, TRAVAIL, SESSION, config)
        reads = [p for p in _valeurs(args, "--allowedTools") if p.startswith("Read")]
        self.assertEqual(reads, [
            f"Read(//srv/sandboxes/alice/.ferme-runs/7/projects/{agent.projet_du_cli(TRAVAIL)}"
            f"/{SESSION}/tool-results/**)"])
        self.assertIn("Read(//srv/sandboxes/alice/.claude/.credentials.json)",
                      _valeurs(args, "--disallowedTools"), "la session claude.ai reste refusée")

    def test_options_du_relais_portent_la_session_et_la_consigne_par_fichier(self):
        consigne = MAISON / agent.CONSIGNES / "1" / "lecture.txt"
        args = agent.options_du_relais(MAISON, TRAVAIL, SESSION, "/tmp/mcp.json", consigne)
        self.assertEqual(_valeurs(args, "--session-id"), [SESSION])
        self.assertEqual(_valeurs(args, "--append-system-prompt-file"), [str(consigne)])
        self.assertNotIn("--append-system-prompt", args)
        self.assertNotIn(agent.CONSIGNE_LECTURE, args)
        self.assertIn("--strict-mcp-config", args)

    def test_le_repertoire_de_travail_n_est_pas_le_home(self):
        self.assertNotEqual(TRAVAIL, MAISON)
        self.assertIn(SESSION, str(TRAVAIL))


if __name__ == "__main__":
    unittest.main()
