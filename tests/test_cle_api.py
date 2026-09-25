"""La connexion par clé d'API, sans systemd : `python3 -m unittest discover tests`.

Ce qui se prouve ici : la clé ne passe jamais par un argv, vit hors des sandboxes, ne
fait basculer aucun run qui ne la demande pas, et un run dont le CLI annonce une autre
source de paiement que celle servie est arrêté. Ce qui ne se prouve que sur la box
(systemd accepte `EnvironmentFile=` sur une unité éphémère, `/proc` ne montre pas la
clé, précédence réelle clé/session du CLI) : voir la procédure du README.
"""
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp(prefix="ferme-test-")
os.environ.update(FERME_TOKEN="x", FERME_HOST="127.0.0.1", FERME_RELAIS_PYTHON="/usr/bin/python3",
                  FERME_RACINE=f"{TMP}/sandboxes", FERME_CLES=f"{TMP}/cles")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent  # noqa: E402

CLE = "sk-ant-api03-" + "A1b2_C3d4-" * 8
COMPTE = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())


class FauxProcessus:
    """Un `claude -p` qui rend des lignes stream-json, puis se termine."""

    def __init__(self, evenements, code=0):
        self.stdout = iter(json.dumps(e) + "\n" for e in evenements)
        self.returncode = None
        self._code = code

    def wait(self, timeout=None):
        self.returncode = self._code
        return self._code

    def poll(self):
        return self.returncode


def init(source):
    return {"type": "system", "subtype": "init", "apiKeySource": source, "model": "claude-sonnet-5"}


RESULTAT = {"type": "result", "result": "ok", "is_error": False}


class Base(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(agent.pwd, "getpwnam", return_value=COMPTE),
                  mock.patch.object(agent, "etat", side_effect=lambda slug: {
                      "loggedIn": True, "apiKey": agent.cle_presente(slug)})):
            p.start()
            self.addCleanup(p.stop)
        agent.fichier_cle("bac").unlink(missing_ok=True)


class ArgvSansCle(Base):
    def argv(self, **kw):
        with mock.patch.object(agent.subprocess, "Popen") as popen:
            agent.dans_le_sandbox("bac", ["-p", "x"], **kw)
        return popen.call_args.args[0]

    def test_la_cle_n_est_servie_que_demandee(self):
        agent.ecrire_cle("bac", CLE)
        self.assertNotIn(f"EnvironmentFile={agent.fichier_cle('bac')}", self.argv())
        self.assertIn(f"EnvironmentFile={agent.fichier_cle('bac')}", self.argv(cle=True))

    def test_la_cle_ne_passe_jamais_par_l_argv(self):
        agent.ecrire_cle("bac", CLE)
        for kw in ({}, {"cle": True}):
            self.assertFalse(any(CLE in a for a in self.argv(**kw)), kw)


class PoseDeLaCle(Base):
    def test_fichier_root_0600_une_seule_ligne_hors_du_home(self):
        agent.ecrire_cle("bac", CLE)
        f = agent.fichier_cle("bac")
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertEqual(f.read_text(), f"ANTHROPIC_API_KEY={CLE}\n")
        self.assertNotIn(agent.RACINE, f.parents)
        self.assertEqual([p.name for p in agent.CLES.iterdir()], ["bac"])   # pas de provisoire laissé

    def test_remplace_la_cle_precedente(self):
        agent.ecrire_cle("bac", CLE)
        autre = CLE[:-4] + "ZZZZ"
        agent.ecrire_cle("bac", autre)
        self.assertEqual(agent.fichier_cle("bac").read_text(), f"ANTHROPIC_API_KEY={autre}\n")

    def test_refuse_un_jeton_de_session_claude_ai(self):
        with self.assertRaises(agent.Erreur) as e:
            agent.ecrire_cle("bac", "sk-ant-oat01-" + "x" * 40)
        self.assertEqual(e.exception.statut, 422)
        self.assertIn("/login", str(e.exception))
        self.assertFalse(agent.cle_presente("bac"))

    def test_refuse_ce_qui_n_est_pas_une_cle(self):
        for mauvaise in ("", "abc", CLE + "\nFERME_TOKEN=vole", CLE + " x", "sk-ant-api"):
            with self.subTest(mauvaise=mauvaise), self.assertRaises(agent.Erreur) as e:
                agent.ecrire_cle("bac", mauvaise)
            self.assertEqual(e.exception.statut, 422)
        self.assertFalse(agent.cle_presente("bac"))

    def test_retirer_la_cle(self):
        agent.ecrire_cle("bac", CLE)
        self.assertFalse(agent.retirer_cle("bac")["apiKey"])
        self.assertFalse(agent.cle_presente("bac"))
        agent.retirer_cle("bac")   # idempotent


class ModeDuRun(Base):
    def test_sans_auth_c_est_la_session_meme_avec_une_cle(self):
        agent.ecrire_cle("bac", CLE)
        self.assertEqual(agent.mode_auth("bac", None), "session")

    def test_key_exige_une_cle_posee(self):
        with self.assertRaises(agent.Erreur) as e:
            agent.mode_auth("bac", "key")
        self.assertEqual(e.exception.statut, 409)
        agent.ecrire_cle("bac", CLE)
        self.assertEqual(agent.mode_auth("bac", "key"), "key")

    def test_auth_inconnu(self):
        with self.assertRaises(agent.Erreur) as e:
            agent.mode_auth("bac", "abonnement")
        self.assertEqual(e.exception.statut, 400)


class SourceConfirmeeParLeCli(Base):
    def run_(self, auth, evenements):
        verrou = mock.Mock()
        with mock.patch.object(agent, "dans_le_sandbox", return_value=FauxProcessus(evenements)) as sb, \
                mock.patch.object(agent.subprocess, "run") as run:
            sortie = list(agent.flux_du_run("bac", verrou, "p", "sonnet", auth=auth))
        verrou.release.assert_called_once()
        return sortie, sb, run

    def test_run_par_cle_confirme(self):
        agent.ecrire_cle("bac", CLE)
        sortie, sb, run = self.run_("key", [init("ANTHROPIC_API_KEY"), RESULTAT])
        self.assertTrue(sb.call_args.kwargs["cle"])
        self.assertEqual(sortie[-1]["auth"], "key")
        self.assertNotIn("erreur", sortie[-1])
        run.assert_not_called()

    def test_run_par_session_confirme(self):
        sortie, sb, _ = self.run_("session", [init("none"), RESULTAT])
        self.assertFalse(sb.call_args.kwargs["cle"])
        self.assertNotIn("erreur", sortie[-1])

    def test_cle_demandee_mais_le_cli_paie_par_session(self):
        agent.ecrire_cle("bac", CLE)
        sortie, _, run = self.run_("key", [init("none"), RESULTAT])
        self.assertTrue(sortie[-1]["erreur"].startswith("auth_mismatch"))
        self.assertNotIn(RESULTAT, sortie)   # rien du run n'a filé après init
        run.assert_called_once_with(["systemctl", "stop", agent.unite_de_run("bac")])

    def test_session_demandee_mais_un_apikeyhelper_paie(self):
        sortie, _, run = self.run_("session", [init("apiKeyHelper"), RESULTAT])
        self.assertTrue(sortie[-1]["erreur"].startswith("auth_mismatch"))
        run.assert_called_once()

    def test_echec_d_un_run_par_cle_ne_dit_pas_needs_login(self):
        agent.ecrire_cle("bac", CLE)
        echec = {"type": "result", "result": "invalid x-api-key", "is_error": True}
        sortie, _, _ = self.run_("key", [init("ANTHROPIC_API_KEY"), echec])
        self.assertTrue(sortie[-1]["connecte"])
        self.assertEqual(sortie[-1]["erreur"], "invalid x-api-key")


class DureeDuRun(Base):
    def argv(self, **kw):
        with mock.patch.object(agent.subprocess, "Popen") as popen:
            agent.dans_le_sandbox("bac", ["-p", "x"], **kw)
        return popen.call_args.args[0]

    def test_la_duree_demandee_passe_apres_les_brides(self):
        argv = self.argv(duree=900)
        valeurs = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"
                   and argv[i + 1].startswith("RuntimeMaxSec=")]
        self.assertEqual(valeurs, ["RuntimeMaxSec=1800", "RuntimeMaxSec=900"])   # la dernière gagne

    def test_sans_demande_les_brides_seules(self):
        self.assertEqual(sum(a.startswith("RuntimeMaxSec=") for a in self.argv()), 1)

    def test_la_demande_est_bornee(self):
        self.assertIsNone(agent.duree_du_run(None))
        self.assertEqual(agent.duree_du_run(10), 60)
        self.assertEqual(agent.duree_du_run(7200), 3600)
        self.assertEqual(agent.duree_du_run(1200), 1200)
        for mauvaise in ("900", 9.5, True):
            with self.subTest(mauvaise=mauvaise), self.assertRaises(agent.Erreur) as e:
                agent.duree_du_run(mauvaise)
            self.assertEqual(e.exception.statut, 400)

    def test_le_run_recoit_sa_duree(self):
        with mock.patch.object(agent, "dans_le_sandbox",
                               return_value=FauxProcessus([init("none"), RESULTAT])) as sb, \
                mock.patch.object(agent.subprocess, "run"):
            list(agent.flux_du_run("bac", mock.Mock(), "p", "sonnet", duree=1200))
        self.assertEqual(sb.call_args.kwargs["duree"], 1200)


if __name__ == "__main__":
    unittest.main()
