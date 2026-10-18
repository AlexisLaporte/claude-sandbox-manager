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
                  FERME_RACINE=f"{TMP}/sandboxes", FERME_CLES=f"{TMP}/cles",
                  FERME_CLES_RUN=f"{TMP}/run-ferme/cles-run")
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
        agent.home("bac").mkdir(parents=True, exist_ok=True)


class ArgvSansCle(Base):
    def argv(self, **kw):
        with mock.patch.object(agent.subprocess, "Popen") as popen:
            agent.dans_le_sandbox("bac", ["-p", "x"], **kw)
        return popen.call_args.args[0]

    def test_la_cle_n_est_servie_que_demandee(self):
        agent.ecrire_cle("bac", CLE)
        self.assertNotIn(f"EnvironmentFile={agent.fichier_cle('bac')}", self.argv())
        self.assertIn(f"EnvironmentFile={agent.fichier_cle('bac')}",
                      self.argv(env=agent.fichier_cle("bac")))

    def test_la_cle_ne_passe_jamais_par_l_argv(self):
        agent.ecrire_cle("bac", CLE)
        for kw in ({}, {"env": agent.fichier_cle("bac")}):
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

    def test_accepte_une_cle_sk_ant_usr(self):
        usr = "sk-ant-usr-" + "A1b2_C3d4-" * 8
        agent.ecrire_cle("bac", usr)
        self.assertEqual(agent.fichier_cle("bac").read_text(), f"ANTHROPIC_API_KEY={usr}\n")

    def test_refuse_ce_qui_n_est_pas_une_cle(self):
        for mauvaise in ("", "abc", CLE + "\nFERME_TOKEN=vole", CLE + " x", "sk-ant-api",
                         "sk-ant-usr", "sk-ant-usr-" + "x" * 40 + "\nFERME_TOKEN=vole"):
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
    def run_(self, auth, evenements, **kw):
        r = agent.Run("bac", auth, **kw)
        self.dernier = r
        with mock.patch.object(agent, "dans_le_sandbox", return_value=FauxProcessus(evenements)) as sb, \
                mock.patch.object(agent.subprocess, "run") as run:
            sortie = list(agent.flux_du_run("bac", r, "p", "sonnet"))
        self.assertEqual(agent.places._value, agent.RUNS_MAX, "la place n'a pas été rendue")
        return sortie, sb, run

    def test_run_par_cle_confirme(self):
        agent.ecrire_cle("bac", CLE)
        sortie, sb, run = self.run_("key", [init("ANTHROPIC_API_KEY"), RESULTAT])
        self.assertEqual(sb.call_args.kwargs["env"], agent.fichier_cle("bac"))
        self.assertEqual(sortie[-1]["auth"], "key")
        self.assertNotIn("erreur", sortie[-1])
        run.assert_not_called()

    def test_run_par_session_confirme(self):
        sortie, sb, _ = self.run_("session", [init("none"), RESULTAT])
        self.assertIsNone(sb.call_args.kwargs["env"])
        self.assertNotIn("erreur", sortie[-1])

    def test_cle_demandee_mais_le_cli_paie_par_session(self):
        agent.ecrire_cle("bac", CLE)
        sortie, _, run = self.run_("key", [init("none"), RESULTAT])
        self.assertTrue(sortie[-1]["erreur"].startswith("auth_mismatch"))
        self.assertNotIn(RESULTAT, sortie)   # rien du run n'a filé après init
        run.assert_called_once_with(["systemctl", "stop", self.dernier.unite])

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
            list(agent.flux_du_run("bac", agent.Run("bac", "session"), "p", "sonnet", duree=1200))
        self.assertEqual(sb.call_args.kwargs["duree"], 1200)



class CleRemiseAvecLeRun(Base):
    """La clé de l'ORG, remise à chaque run par le worker : aucune copie ne reste."""

    def lancer(self, evenements, **kw):
        effort = kw.pop("effort", None)
        r = agent.Run("bac", "key", **kw)
        vu = {}

        def sandbox(slug, args, **k):
            vu.update(k, args=args, contenu=k["env"].read_text() if k.get("env") else None,
                      config_existe=bool(k.get("config")) and k["config"].is_dir())
            return FauxProcessus(evenements)
        with mock.patch.object(agent, "dans_le_sandbox", side_effect=sandbox), \
                mock.patch.object(agent.subprocess, "run"):
            sortie = list(agent.flux_du_run("bac", r, "p", "sonnet", effort=effort))
        return r, vu, sortie

    def test_la_cle_du_run_vit_le_temps_du_run(self):
        r, vu, sortie = self.lancer([init("ANTHROPIC_API_KEY"), RESULTAT], cle_du_run=CLE,
                                    workspace="wrkspc_01")
        self.assertEqual(vu["contenu"], f"ANTHROPIC_API_KEY={CLE}\n"
                         "ANTHROPIC_CUSTOM_HEADERS=anthropic-workspace-id: wrkspc_01\n")
        self.assertNotIn(agent.RACINE, vu["env"].parents)          # hors du sandbox
        self.assertFalse(vu["env"].exists(), "la clé du run doit être effacée à la fin")
        self.assertTrue(vu["config_existe"])
        self.assertFalse(vu["config"].exists(), "la config du run doit être effacée à la fin")
        self.assertNotIn("erreur", sortie[-1])
        self.assertFalse(agent.cle_presente("bac"), "une clé du run n'est jamais stockée")

    def test_la_cle_du_run_ne_passe_jamais_par_l_argv(self):
        r = agent.Run("bac", "key", cle_du_run=CLE)
        try:
            with mock.patch.object(agent.subprocess, "Popen") as popen:
                agent.dans_le_sandbox("bac", ["-p", "x"], unite=r.unite, env=r.env, config=r.config)
            self.assertFalse(any(CLE in a for a in popen.call_args.args[0]))
        finally:
            r.liberer()

    def test_les_runs_par_cle_tournent_en_parallele_chacun_dans_son_unite(self):
        a = agent.Run("bac", "key", cle_du_run=CLE)
        b = agent.Run("bac", "key", cle_du_run=CLE)
        try:
            self.assertNotEqual(a.unite, b.unite)
            self.assertNotEqual(a.config, b.config)
            self.assertNotEqual(a.env, b.env)
        finally:
            a.liberer(); b.liberer()

    def test_la_session_reste_un_run_a_la_fois(self):
        a = agent.Run("bac", "session")
        try:
            with self.assertRaises(agent.Erreur) as e:
                agent.Run("bac", "session")
            self.assertEqual(e.exception.statut, 409)
            agent.Run("bac", "key", cle_du_run=CLE).liberer()   # une clé ne l'attend pas
        finally:
            a.liberer()

    def test_la_box_refuse_au_dela_de_ses_places(self):
        tenus = [agent.Run("bac", "key", cle_du_run=CLE) for _ in range(agent.RUNS_MAX)]
        try:
            with self.assertRaises(agent.Erreur) as e:
                agent.Run("bac", "key", cle_du_run=CLE)
            self.assertEqual(e.exception.statut, 429)
        finally:
            for t in tenus:
                t.liberer()
        agent.Run("bac", "key", cle_du_run=CLE).liberer()           # les places sont revenues
        self.assertEqual(agent.places._value, agent.RUNS_MAX)

    def test_un_refus_a_la_reservation_ne_garde_rien(self):
        with self.assertRaises(agent.Erreur):
            agent.Run("bac", "key", cle_du_run="sk-ant-oat01-" + "x" * 40)
        self.assertEqual(agent.places._value, agent.RUNS_MAX)
        self.assertEqual([p for p in agent.CLES.iterdir() if ".run-" in p.name], [])
        with self.assertRaises(agent.Erreur) as e:
            agent.Run("bac", "key", cle_du_run=CLE, workspace="a b")
        self.assertEqual(e.exception.statut, 422)
        self.assertEqual(agent.places._value, agent.RUNS_MAX)

    def test_le_mode_de_la_cle_du_run(self):
        self.assertEqual(agent.mode_auth("bac", "key", CLE), "key")   # sans clé stockée
        for demande in (None, "session"):
            with self.subTest(demande=demande), self.assertRaises(agent.Erreur) as e:
                agent.mode_auth("bac", demande, CLE)
            self.assertEqual(e.exception.statut, 400)

    def test_l_effort_part_sauf_none(self):
        _, vu, _ = self.lancer([init("ANTHROPIC_API_KEY"), RESULTAT], cle_du_run=CLE, effort="high")
        self.assertIn("--effort", vu["args"])
        self.assertEqual(vu["args"][vu["args"].index("--effort") + 1], "high")
        _, vu, _ = self.lancer([init("ANTHROPIC_API_KEY"), RESULTAT], cle_du_run=CLE, effort="none")
        self.assertNotIn("--effort", vu["args"])



class RienNeFuit(Base):
    """Une place de la box et une copie de clé ne survivent jamais à leur run."""

    def _handler(self, casse):
        h = agent.Handler.__new__(agent.Handler)
        h.headers = {"Accept": "application/x-ndjson"}

        def boum(*a, **k):
            raise BrokenPipeError("client parti")
        h.send_response = boum if casse == "entetes" else (lambda *a, **k: None)
        h.send_header = lambda *a, **k: None
        h.end_headers = lambda *a, **k: None
        h.wfile = mock.Mock(write=boum if casse == "ecriture" else mock.Mock(), flush=mock.Mock())
        return h

    def _essai(self, casse):
        corps = {"prompt": "p", "auth": "key", "api_key": CLE}
        with mock.patch.object(agent, "dans_le_sandbox",
                               return_value=FauxProcessus([init("ANTHROPIC_API_KEY"), RESULTAT])), \
                mock.patch.object(agent.subprocess, "run"), self.assertRaises(BrokenPipeError):
            self._handler(casse)._run("bac", corps)
        self.assertEqual(agent.places._value, agent.RUNS_MAX, "la place a fui")
        for d in (agent.CLES, agent.CLES_RUN):
            restes = [f for f in d.iterdir() if ".run-" in f.name] if d.is_dir() else []
            self.assertEqual(restes, [], f"la clé du run est restée sur le disque ({d})")

    def test_client_parti_avant_le_premier_octet(self):
        self._essai("entetes")

    def test_ecriture_qui_casse_en_vol(self):
        self._essai("ecriture")

    def test_le_demarrage_efface_les_restes_d_un_arret_en_plein_run(self):
        reste_cle = agent.CLES / "bac.run-abc"
        agent.CLES.mkdir(parents=True, exist_ok=True)
        reste_cle.write_text("ANTHROPIC_API_KEY=x\n")
        agent.ecrire_cle("bac", CLE)                      # une clé STOCKÉE, elle, reste
        reste_config = agent.home("bac") / ".ferme-runs" / "abc"
        reste_config.mkdir(parents=True)
        agent.nettoyer_restes()
        self.assertFalse(reste_cle.exists())
        self.assertFalse(reste_config.exists())
        self.assertTrue(agent.cle_presente("bac"))


class CorpsIncomplet(Base):
    """Un corps de run incomplet se refuse AVANT de réserver quoi que ce soit."""

    def _handler(self):
        h = agent.Handler.__new__(agent.Handler)
        h.headers = {"Accept": "application/x-ndjson"}
        return h

    def _refuse(self, corps):
        with mock.patch.object(agent, "dans_le_sandbox") as sandbox:
            with self.assertRaises(agent.Erreur) as e:
                self._handler()._run("bac", corps)
        self.assertEqual(e.exception.statut, 400)
        sandbox.assert_not_called()
        self.assertEqual(agent.places._value, agent.RUNS_MAX, "la place a fui")
        for d in (agent.CLES, agent.CLES_RUN):
            restes = [f for f in d.iterdir() if ".run-" in f.name] if d.is_dir() else []
            self.assertEqual(restes, [], f"une clé de run est restée ({d})")

    def test_sans_prompt(self):
        self._refuse({"auth": "key", "api_key": CLE})

    def test_prompt_qui_n_est_pas_un_texte(self):
        self._refuse({"auth": "key", "api_key": CLE, "prompt": ["x"]})

    def test_prompt_vide(self):
        self._refuse({"auth": "key", "api_key": CLE, "prompt": "  "})


class LiensDuSandbox(Base):
    """Le HOME est au sandbox : un lien qu'il y pose ne doit jamais être suivi par root."""

    def setUp(self):
        super().setUp()
        self.menage()
        self.addCleanup(self.menage)
        self.victime = Path(tempfile.mkdtemp(prefix="victime-", dir=TMP))
        (self.victime / "garde").mkdir()
        (self.victime / "garde" / "fichier").write_text("précieux")

    @staticmethod
    def menage():
        import shutil
        for nom in (".ferme-runs", ".ferme-runs-deplace"):
            d = agent.home("bac") / nom
            if d.is_symlink() or d.is_file():
                d.unlink()
            elif d.is_dir():
                shutil.rmtree(d)

    def contenu_victime(self):
        return sorted(str(p.relative_to(self.victime)) for p in self.victime.rglob("*"))

    def test_un_lien_ferme_runs_refuse_le_run_sans_toucher_la_cible(self):
        avant = self.contenu_victime()
        (agent.home("bac") / ".ferme-runs").symlink_to(self.victime)
        with self.assertRaises(agent.Erreur):
            agent.Run("bac", "key", cle_du_run=CLE)
        self.assertEqual(self.contenu_victime(), avant, "root a écrit à travers le lien")
        self.assertEqual(agent.places._value, agent.RUNS_MAX, "la place a fui")

    def test_la_liberation_ne_suit_pas_un_lien_pose_pendant_le_run(self):
        r = agent.Run("bac", "key", cle_du_run=CLE)
        (self.victime / r.ident).mkdir()
        (self.victime / r.ident / "fichier").write_text("précieux")
        avant = self.contenu_victime()
        runs = agent.home("bac") / ".ferme-runs"
        runs.rename(agent.home("bac") / ".ferme-runs-deplace")
        runs.symlink_to(self.victime)
        r.liberer()
        self.assertEqual(self.contenu_victime(), avant, "root a effacé à travers le lien")

    def test_le_demarrage_ne_suit_pas_un_lien(self):
        avant = self.contenu_victime()
        (agent.home("bac") / ".ferme-runs").symlink_to(self.victime)
        agent.nettoyer_restes()
        self.assertEqual(self.contenu_victime(), avant, "root a vidé la cible du lien")

    def test_la_config_mcp_ne_suit_pas_de_lien(self):
        with mock.patch.object(agent.os, "chown", side_effect=AssertionError(
                "chown par chemin : suivrait un lien posé après la création")):
            chemin = agent.ecrire_config_mcp("bac", {"url": "u", "token": "t"})
        self.assertFalse(chemin.is_symlink())
        chemin.unlink()


class DurcissementsDeLaBox(Base):
    def test_la_cle_d_un_run_vit_en_memoire_hors_de_etc(self):
        r = agent.Run("bac", "key", cle_du_run=CLE)
        try:
            self.assertEqual(r.env_du_run.parent, agent.CLES_RUN)
            self.assertNotEqual(agent.CLES_RUN, agent.CLES)
        finally:
            r.liberer()

    def test_les_brides_cachent_les_cles_de_run_et_les_processus_des_autres(self):
        brides = " ".join(agent.BRIDES)
        self.assertIn(f"-{agent.CLES_RUN.parent}", brides)
        self.assertIn("ProtectProc=invisible", agent.BRIDES)


if __name__ == "__main__":
    unittest.main()
