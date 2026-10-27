"""Un run à relais délègue à un sous-agent, et l'attend (claude-sandbox-manager#2) ; les
bornes d'un run (tours, dollars, modèle des sous-agents).

Ce qui ne se prouve que sur la box : un sous-agent (claude-code-guide, general-purpose)
qui tente Bash ou WebFetch est refusé — voir la procédure du README."""
import unittest
from unittest import mock

from test_cle_api import RESULTAT, Base, FauxProcessus, agent, init


def valeurs(args, option):
    """Les valeurs qui suivent `option` jusqu'à l'option suivante."""
    i = args.index(option) + 1
    fin = next((j for j in range(i, len(args)) if args[j].startswith("--")), len(args))
    return args[i:fin]


class SousAgents(Base):
    def lancer(self, **kw):
        vu = {}

        def sandbox(slug, args, **k):
            vu.update(k, args=args)
            return FauxProcessus([init("none"), RESULTAT])
        with mock.patch.object(agent, "dans_le_sandbox", side_effect=sandbox), \
                mock.patch.object(agent.subprocess, "run"):
            list(agent.flux_du_run("bac", agent.Run("bac", "session"), "p", "sonnet",
                                   mcp={"url": "u", "token": "t"}, **kw))
        return vu

    def test_le_seul_outil_integre_est_la_delegation(self):
        args = self.lancer()["args"]
        self.assertEqual(valeurs(args, "--tools"), ["Task,Read"])   # Read : cf. test_outils_du_run
        self.assertEqual(valeurs(args, "--permission-mode"), ["dontAsk"])
        self.assertNotIn("Bash", " ".join(args))

    def test_un_run_attend_ses_sous_agents(self):
        with mock.patch.object(agent.subprocess, "Popen") as popen:
            agent.dans_le_sandbox("bac", ["-p", "x"])
        argv = popen.call_args.args[0]
        self.assertIn("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0", argv)
        self.assertEqual(argv[argv.index("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0") - 1], "-E")


class Bornes(Base):
    """Tours, dollars, modèle des sous-agents : un run s'arrête avant d'épuiser la clé."""

    def lancer(self, evenements=None, **kw):
        vu = {}

        def sandbox(slug, args, **k):
            vu.update(k, args=args)
            return FauxProcessus(evenements or [init("none"), RESULTAT])
        with mock.patch.object(agent, "dans_le_sandbox", side_effect=sandbox), \
                mock.patch.object(agent.subprocess, "run"):
            vu["sortie"] = list(agent.flux_du_run("bac", agent.Run("bac", "session"), "p",
                                                  "sonnet", **kw))
        return vu

    def test_un_run_sans_demande_n_a_aucune_borne(self):
        args = self.lancer()["args"]
        self.assertNotIn("--max-turns", args)
        self.assertNotIn("--max-budget-usd", args)

    def test_les_sous_agents_tournent_sur_le_modele_du_run(self):
        for mcp in (None, {"url": "u", "token": "t"}):
            with self.subTest(mcp=bool(mcp)):
                variables = self.lancer(mcp=mcp)["variables"]
                self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=inherit", variables)
                self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1", variables)

    def test_la_demande_passe_telle_quelle(self):
        self.assertIsNone(agent.tours_du_run(None))
        self.assertEqual(agent.tours_du_run(5000), 5000)
        self.assertIsNone(agent.budget_du_run(None))
        self.assertEqual(agent.budget_du_run(1000), 1000.0)
        self.assertEqual(agent.budget_du_run(12), 12.0)
        for f, mauvaise in ((agent.tours_du_run, "50"), (agent.tours_du_run, 2.5),
                            (agent.tours_du_run, True), (agent.tours_du_run, 0),
                            (agent.budget_du_run, "5"), (agent.budget_du_run, False),
                            (agent.budget_du_run, float("nan")), (agent.budget_du_run, 0)):
            with self.subTest(f=f.__name__, mauvaise=mauvaise), self.assertRaises(agent.Erreur) as e:
                f(mauvaise)
            self.assertEqual(e.exception.statut, 400)

    def test_une_demande_invalide_est_refusee_avant_de_reserver(self):
        h = agent.Handler.__new__(agent.Handler)
        h.headers = {}
        with mock.patch.object(agent, "dans_le_sandbox") as sandbox, \
                self.assertRaises(agent.Erreur):
            h._run("bac", {"prompt": "p", "max_budget_usd": "beaucoup"})
        sandbox.assert_not_called()
        self.assertEqual(agent.places.libres(), agent.RUNS_MAX, "la place a fui")

    def test_la_demande_du_run_arrive_au_cli(self):
        h = agent.Handler.__new__(agent.Handler)
        h.headers = {}
        vu = {}

        def sandbox(slug, args, **k):
            vu["args"] = args
            return FauxProcessus([init("none"), RESULTAT])
        with mock.patch.object(agent, "dans_le_sandbox", side_effect=sandbox), \
                mock.patch.object(agent.subprocess, "run"):
            h._run("bac", {"prompt": "p", "max_turns": 30, "max_budget_usd": 2.5})
        self.assertEqual(valeurs(vu["args"], "--max-turns"), ["30"])
        self.assertEqual(valeurs(vu["args"], "--max-budget-usd"), ["2.5"])

    def test_une_borne_atteinte_se_dit_dans_le_resume(self):
        borne = {"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                 "errors": ["Reached maximum budget ($0.1)"]}
        fin = self.lancer([init("none"), borne])["sortie"][-1]
        self.assertEqual(fin["erreur"], "Reached maximum budget ($0.1)")


if __name__ == "__main__":
    unittest.main()
