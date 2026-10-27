"""Le répertoire de travail d'un run à relais et ce que le CLI y range (claude-sandbox-manager#3,
repris sur #1) : créé et effacé par root sans jamais suivre un lien du sandbox, suivi du bon
dossier de config sur la voie par clé, purgé au démarrage."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_cle_api import CLE, RESULTAT, TMP, Base, FauxProcessus, agent, init

MCP = {"url": "u", "token": "t"}


class TravailDuRun(Base):
    def setUp(self):
        super().setUp()
        self.menage()
        self.addCleanup(self.menage)
        self.victime = Path(tempfile.mkdtemp(prefix="victime-", dir=TMP))
        (self.victime / "garde").mkdir()
        (self.victime / "garde" / "fichier").write_text("précieux")

    @staticmethod
    def menage():
        for nom in (agent.RUNS, agent.TRAVAIL, ".claude", ".ferme-travail-deplace"):
            d = agent.home("bac") / nom
            if d.is_symlink() or d.is_file():
                d.unlink()
            elif d.is_dir():
                shutil.rmtree(d)

    def contenu_victime(self):
        return sorted(str(p.relative_to(self.victime)) for p in self.victime.rglob("*"))

    def lancer(self, auth, source, pendant=None):
        """Un run à relais ; `pendant(vu)` joue le CLI pendant le run (ranger un résultat…)."""
        r = agent.Run("bac", auth, **({"cle_du_run": CLE} if auth == "key" else {}))
        vu = {"run": r}

        def sandbox(slug, args, **k):
            vu.update(k, args=args)
            if pendant:
                pendant(vu)
            return FauxProcessus([init(source), RESULTAT])
        with mock.patch.object(agent, "dans_le_sandbox", side_effect=sandbox), \
                mock.patch.object(agent.subprocess, "run"):
            list(agent.flux_du_run("bac", r, "p", "sonnet", mcp=MCP))
        return vu

    @staticmethod
    def ranger(vu):
        """Ce que le CLI range pour la session : `<config>/projects/<projet>/<session>/tool-results`."""
        args = vu["args"]
        session = args[args.index("--session-id") + 1]
        config = vu["config"] or agent.home("bac") / ".claude"
        d = config / "projects" / agent.projet_du_cli(vu["repertoire"]) / session / "tool-results"
        d.mkdir(parents=True)
        (d / "gros.txt").write_text("données du client")
        vu["range"] = d

    # 2 — `variables` et `env` ne se confondent plus
    def test_variables_et_fichier_d_environnement_coexistent(self):
        with mock.patch.object(agent.subprocess, "Popen") as popen:
            agent.dans_le_sandbox("bac", ["-p", "x"], env=agent.CLES_RUN / "bac.run-1",
                                  variables=("MAX_MCP_OUTPUT_TOKENS=10000",))
        argv = popen.call_args.args[0]
        self.assertIn(f"EnvironmentFile={agent.CLES_RUN / 'bac.run-1'}", argv)
        i = argv.index("MAX_MCP_OUTPUT_TOKENS=10000")
        self.assertEqual(argv[i - 1], "-E")

    def test_le_run_a_relais_tourne_dans_son_repertoire_avec_sa_limite(self):
        vu = self.lancer("session", "none")
        self.assertEqual(vu["repertoire"].parent, agent.home("bac") / agent.TRAVAIL)
        self.assertIn(f"MAX_MCP_OUTPUT_TOKENS={agent.MCP_JETONS_MAX}", vu["variables"])

    # 3 — sur la voie par clé, `Read` suit le dossier de config du run, effacé avant `liberer`
    def test_par_cle_read_vise_la_config_du_run(self):
        vu = self.lancer("key", "ANTHROPIC_API_KEY")
        permis = [a for a in vu["args"] if a.startswith("Read(")]
        self.assertTrue(permis[0].startswith(f"Read(/{vu['config']}/projects/"), permis[0])

    def test_par_cle_le_travail_s_efface_avant_la_liberation(self):
        ordre = []
        vrai = agent.effacer_le_travail

        def effacer(slug, travail, config=None):
            ordre.append(("effacer", config, self.courant.libere, config.is_dir()))
            vrai(slug, travail, config)
        with mock.patch.object(agent, "effacer_le_travail", side_effect=effacer):
            vu = self.lancer("key", "ANTHROPIC_API_KEY",
                             pendant=lambda vu: setattr(self, "courant", vu["run"]) or self.ranger(vu))
        self.assertEqual(ordre, [("effacer", vu["config"], False, True)])
        self.assertFalse(vu["repertoire"].exists())
        self.assertEqual(agent.places.libres(), agent.RUNS_MAX)

    def test_par_session_les_resultats_ranges_sont_effaces_et_rien_d_autre(self):
        autre = agent.home("bac") / ".claude" / "projects" / "-un-autre-projet"
        autre.mkdir(parents=True)
        vu = self.lancer("session", "none", pendant=self.ranger)
        self.assertFalse(vu["range"].exists())
        self.assertFalse(vu["repertoire"].exists())
        self.assertTrue(autre.is_dir(), "un autre projet du CLI a été effacé")

    # 4 — le démarrage purge travail et résultats rangés
    def test_le_demarrage_purge_le_travail_et_les_resultats(self):
        travail = agent.home("bac") / agent.TRAVAIL / "s1"
        travail.mkdir(parents=True)
        range_ = agent.home("bac") / ".claude" / "projects" / agent.projet_du_cli(travail)
        (range_ / "s1" / "tool-results").mkdir(parents=True)
        autre = agent.home("bac") / ".claude" / "projects" / "-un-autre-projet"
        autre.mkdir(parents=True)
        agent.nettoyer_restes()
        self.assertFalse(travail.exists())
        self.assertFalse(range_.exists())
        self.assertTrue(autre.is_dir())

    def test_le_demarrage_ne_suit_ni_travail_ni_claude_en_lien(self):
        avant = self.contenu_victime()
        (agent.home("bac") / agent.TRAVAIL).symlink_to(self.victime)
        (agent.home("bac") / ".claude").symlink_to(self.victime)
        agent.nettoyer_restes()
        self.assertEqual(self.contenu_victime(), avant, "root a vidé la cible d'un lien")

    # 5 — le répertoire de travail se crée par descripteur
    def test_un_lien_ferme_travail_refuse_le_run_sans_toucher_la_cible(self):
        avant = self.contenu_victime()
        (agent.home("bac") / agent.TRAVAIL).symlink_to(self.victime)
        with self.assertRaises(agent.Erreur):
            self.lancer("session", "none")
        self.assertEqual(self.contenu_victime(), avant, "root a écrit à travers le lien")
        self.assertEqual(agent.places.libres(), agent.RUNS_MAX, "la place a fui")

    def test_l_effacement_ne_suit_pas_un_lien_pose_pendant_le_run(self):
        def poser(vu):
            session = vu["repertoire"].name
            (self.victime / session).mkdir()
            (self.victime / session / "fichier").write_text("précieux")
            self.avant = self.contenu_victime()
            travail = agent.home("bac") / agent.TRAVAIL
            travail.rename(agent.home("bac") / ".ferme-travail-deplace")
            travail.symlink_to(self.victime)
            (agent.home("bac") / ".claude").mkdir(exist_ok=True)
        self.lancer("session", "none", pendant=poser)
        self.assertEqual(self.contenu_victime(), self.avant, "root a effacé à travers le lien")

    def test_la_creation_ne_chown_jamais_par_chemin(self):
        with mock.patch.object(agent.os, "chown", wraps=agent.os.chown) as chown:
            agent.creer_dossier_du_run("bac", agent.TRAVAIL, "s2")
        for appel in chown.call_args_list:
            self.assertIn("dir_fd", appel.kwargs)
            self.assertFalse(appel.kwargs.get("follow_symlinks", True))


if __name__ == "__main__":
    unittest.main()
