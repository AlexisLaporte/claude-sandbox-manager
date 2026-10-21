"""Aucun texte d'un run en argv : ni le prompt, ni le prompt système, ni la consigne de
lecture. L'argv d'une unité se lit par D-Bus (`systemctl show`, `list-units`), dans
/proc et dans le journal — par toute la box, les sandboxes des autres orgs compris.

Ce qui ne se prouve que sur la box (un sandbox, sous les brides réelles, ne liste pas
les unités et ne lit pas l'`ExecStart` d'une autre) : `tests/isolation.py`."""
import stat
import unittest
from pathlib import Path
from unittest import mock

from test_cle_api import RESULTAT, Base, FauxProcessus, agent, init

PROMPT = "Consigne factice du run, avec des espaces et un accent : été."
SYSTEME = "Procédure factice : ne fais rien d'autre que répondre."
MCP = {"url": "u", "token": "t"}


class ArgvSansTexte(Base):
    def lancer(self, conforme=True, **kw):
        vu = {}

        def sandbox(slug, args, **k):
            vu.update(k, args=list(args))
            fichiers = [a for a in args if a.endswith(".txt")]
            vu["lus"] = {Path(f).name: (Path(f).read_text(), stat.S_IMODE(Path(f).stat().st_mode))
                         for f in fichiers}
            return FauxProcessus([init("none"), RESULTAT])
        with mock.patch.object(agent, "dans_le_sandbox", side_effect=sandbox), \
                mock.patch.object(agent, "unite_conforme", return_value=conforme), \
                mock.patch.object(agent.subprocess, "run") as run:
            vu["sortie"] = list(agent.flux_du_run("bac", agent.Run("bac", "session"), PROMPT,
                                                  "sonnet", system=SYSTEME, **kw))
        vu["run"] = run
        return vu

    def test_aucun_texte_du_run_dans_l_argv(self):
        for kw in ({}, {"mcp": MCP}):
            with self.subTest(**kw):
                vu = self.lancer(**kw)
                for texte in (PROMPT, SYSTEME, agent.CONSIGNE_LECTURE):
                    self.assertFalse(any(texte in a for a in vu["args"]), texte)
                self.assertTrue(all(agent.ELEMENT_D_ARGV.fullmatch(a) for a in vu["args"]))

    def test_le_prompt_passe_sur_stdin(self):
        self.assertEqual(self.lancer()["entree"], PROMPT)

    def test_les_textes_passent_par_des_fichiers_0600_effaces_en_fin_de_run(self):
        vu = self.lancer(mcp=MCP)
        self.assertEqual(vu["lus"], {"systeme.txt": (SYSTEME, 0o600),
                                     "lecture.txt": (agent.CONSIGNE_LECTURE, 0o600)})
        self.assertIn("--system-prompt-file", vu["args"])
        self.assertIn("--append-system-prompt-file", vu["args"])
        self.assertEqual(list((agent.home("bac") / agent.CONSIGNES).iterdir()), [],
                         "les textes du run sont restés sur le disque")

    def test_une_unite_non_conforme_est_arretee_avant_tout_tour(self):
        vu = self.lancer(conforme=False)
        self.assertIn("argv_non_conforme", vu["sortie"][-1]["erreur"])
        vu["run"].assert_called_with(["systemctl", "stop", agent.unite_de_run("bac")])

    def test_le_demarrage_efface_les_textes_restes(self):
        reste = agent.home("bac") / agent.CONSIGNES / "abc"
        reste.mkdir(parents=True)
        (reste / "systeme.txt").write_text(SYSTEME)
        agent.nettoyer_restes()
        self.assertFalse(reste.exists())


class LancementDeLUnite(Base):
    def lancer(self, args, entree=None):
        with mock.patch.object(agent.subprocess, "Popen") as popen:
            p = agent.dans_le_sandbox("bac", args, entree=entree)
        return popen, p

    def test_un_texte_en_argv_est_refuse_sans_rien_lancer(self):
        with mock.patch.object(agent.subprocess, "Popen") as popen, \
                self.assertRaises(agent.Erreur) as e:
            agent.dans_le_sandbox("bac", ["-p", PROMPT])
        self.assertEqual(e.exception.statut, 500)
        popen.assert_not_called()

    def test_la_description_ne_porte_que_le_bac(self):
        popen, _ = self.lancer(["-p"])
        self.assertIn("--description=ferme:bac", popen.call_args.args[0])

    def test_l_entree_est_ecrite_puis_fermee(self):
        _, p = self.lancer(["-p"], entree=PROMPT)
        p.stdin.write.assert_called_once_with(PROMPT)
        p.stdin.close.assert_called_once_with()


class UniteConforme(unittest.TestCase):
    ARGS = ["-p", "--model", "sonnet"]

    def show(self, sortie):
        with mock.patch.object(agent.subprocess, "run",
                               return_value=mock.Mock(stdout=sortie)) as run:
            vu = agent.unite_conforme("sandbox-run-bac", "bac", self.ARGS)
        self.assertEqual(run.call_args.args[0][:3], ["systemctl", "show", "sandbox-run-bac"])
        return vu

    def execstart(self, *args):
        return (f"ExecStart={{ path={agent.CLAUDE} ; argv[]={' '.join([agent.CLAUDE, *args])} ; "
                f"ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=1 ; code=(null) ; "
                f"status=0/0 }}\n")

    def test_la_commande_attendue_et_la_description(self):
        self.assertTrue(self.show(self.execstart(*self.ARGS) + "Description=ferme:bac\n"))

    def test_un_argument_de_plus_n_est_pas_conforme(self):
        self.assertFalse(self.show(self.execstart(*self.ARGS, "texte") + "Description=ferme:bac\n"))

    def test_une_autre_description_n_est_pas_conforme(self):
        self.assertFalse(self.show(self.execstart(*self.ARGS) + f"Description={agent.CLAUDE} -p\n"))


if __name__ == "__main__":
    unittest.main()
