"""Un run à relais délègue à un sous-agent, et l'attend (claude-sandbox-manager#2).

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
        self.assertEqual(valeurs(args, "--tools"), ["Task"])
        self.assertEqual(valeurs(args, "--permission-mode"), ["dontAsk"])
        self.assertNotIn("Bash", " ".join(args))

    def test_un_run_attend_ses_sous_agents(self):
        with mock.patch.object(agent.subprocess, "Popen") as popen:
            agent.dans_le_sandbox("bac", ["-p", "x"])
        argv = popen.call_args.args[0]
        self.assertIn("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0", argv)
        self.assertEqual(argv[argv.index("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0") - 1], "-E")


if __name__ == "__main__":
    unittest.main()
