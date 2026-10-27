"""La file d'attente des places : un worker attend son tour AVANT de prendre un travail,
puis son run consomme la place réservée — plus de travail pris puis refusé en 429."""
import threading
import time
from unittest import mock

from test_cle_api import CLE, Base, agent


class FileDesPlaces(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.vider)

    @staticmethod
    def vider():
        for jeton in list(agent.reservations):
            agent.rendre_place(jeton)

    def tenir_tout(self):
        tenus = [agent.Run("bac", "key", cle_du_run=CLE) for _ in range(agent.RUNS_MAX)]
        self.addCleanup(lambda: [t.liberer() for t in tenus])
        return tenus

    def test_une_place_libre_est_reservee_tout_de_suite(self):
        r = agent.reserver_place(0)
        self.assertRegex(r["place"], r"^[0-9a-f]{32}$")
        self.assertEqual(r["tenue_s"], agent.PLACE_TENUE_S)
        self.assertEqual(agent.places._value, agent.RUNS_MAX - 1)

    def test_le_run_consomme_la_place_reservee_sans_en_prendre_une_autre(self):
        jetons = [agent.reserver_place(0)["place"] for _ in range(agent.RUNS_MAX)]
        self.assertEqual(agent.places._value, 0)
        run = agent.Run("bac", "key", cle_du_run=CLE, place=jetons[0])   # la box est pleine
        self.assertNotIn(jetons[0], agent.reservations)
        run.liberer()
        self.assertEqual(agent.places._value, 1)

    def test_box_pleine_l_attente_rend_aucune_place_sans_rien_garder(self):
        self.tenir_tout()
        debut = time.monotonic()
        self.assertEqual(agent.reserver_place(0.2), {"place": None})
        self.assertGreaterEqual(time.monotonic() - debut, 0.2)
        self.assertEqual(agent.reservations, {})

    def test_l_attente_prend_la_place_des_qu_un_run_finit(self):
        tenus = self.tenir_tout()
        vu = {}
        t = threading.Thread(target=lambda: vu.update(agent.reserver_place(5)))
        t.start()
        time.sleep(0.1)
        self.assertEqual(vu, {})                       # elle attend, sans 429
        tenus.pop().liberer()
        t.join(2)
        self.assertIsNotNone(vu.get("place"))

    def test_sans_travail_le_worker_rend_la_place(self):
        jeton = agent.reserver_place(0)["place"]
        self.assertEqual(agent.rendre_place(jeton), {"rendue": True})
        self.assertEqual(agent.rendre_place(jeton), {"rendue": False})   # une seule fois
        self.assertEqual(agent.places._value, agent.RUNS_MAX)

    def test_une_place_jamais_consommee_revient_a_la_box(self):
        jeton = agent.reserver_place(0)["place"]
        self.assertEqual(agent.echoir_reservations(time.monotonic()), 0)
        self.assertEqual(agent.echoir_reservations(time.monotonic() + agent.PLACE_TENUE_S + 1), 1)
        self.assertEqual(agent.places._value, agent.RUNS_MAX)
        # échue : le run en demande une comme s'il n'en apportait pas
        agent.Run("bac", "key", cle_du_run=CLE, place=jeton).liberer()
        self.assertEqual(agent.places._value, agent.RUNS_MAX)

    def test_une_place_inconnue_box_pleine_reste_un_429(self):
        self.tenir_tout()
        for place in ("0" * 32, None, {"x": 1}):
            with self.subTest(place=place), self.assertRaises(agent.Erreur) as e:
                agent.Run("bac", "key", cle_du_run=CLE, place=place)
            self.assertEqual(e.exception.statut, 429)

    def test_un_run_refuse_rend_sa_place_reservee(self):
        jeton = agent.reserver_place(0)["place"]
        h = agent.Handler.__new__(agent.Handler)
        with self.assertRaises(agent.Erreur):
            h._run("bac", {"prompt": " ", "place": jeton})
        self.assertEqual(agent.reservations, {})
        self.assertEqual(agent.places._value, agent.RUNS_MAX)

    def test_l_attente_demandee_est_bornee(self):
        for attente in (-1, agent.ATTENTE_MAX_S + 1, "beaucoup"):
            with self.subTest(attente=attente), self.assertRaises(agent.Erreur) as e:
                agent.reserver_place(attente)
            self.assertEqual(e.exception.statut, 400)

    def test_les_routes(self):
        vu = []
        h = agent.Handler.__new__(agent.Handler)
        h.headers = {"Authorization": f"Bearer {agent.JETON}"}
        h._repondre = lambda statut, corps, *a: vu.append((statut, corps))
        h._corps = lambda: {"attente_s": 0}
        h.path = "/api/places"
        h._router("POST")
        statut, corps = vu.pop()
        self.assertEqual(statut, 200)
        h.path = f"/api/places/{corps['place']}"
        h._router("DELETE")
        self.assertEqual(vu.pop(), (200, {"rendue": True}))
        h._router("POST")
        self.assertEqual(vu.pop()[0], 405)
        h.headers = {}
        h._router("DELETE")
        self.assertEqual(vu.pop()[0], 401)
