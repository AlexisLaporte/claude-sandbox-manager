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
        self.assertEqual(agent.places.libres(), agent.RUNS_MAX - 1)

    def test_le_run_consomme_la_place_reservee_sans_en_prendre_une_autre(self):
        jetons = [agent.reserver_place(0)["place"] for _ in range(agent.RUNS_MAX)]
        self.assertEqual(agent.places.libres(), 0)
        run = agent.Run("bac", "key", cle_du_run=CLE, place=jetons[0])   # la box est pleine
        self.assertNotIn(jetons[0], agent.reservations)
        run.liberer()
        self.assertEqual(agent.places.libres(), 1)

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
        self.assertEqual(agent.places.libres(), agent.RUNS_MAX)

    def test_une_place_jamais_consommee_revient_a_la_box(self):
        jeton = agent.reserver_place(0)["place"]
        self.assertEqual(agent.echoir_reservations(time.monotonic()), 0)
        self.assertEqual(agent.echoir_reservations(time.monotonic() + agent.PLACE_TENUE_S + 1), 1)
        self.assertEqual(agent.places.libres(), agent.RUNS_MAX)
        # échue : le run en demande une comme s'il n'en apportait pas
        agent.Run("bac", "key", cle_du_run=CLE, place=jeton).liberer()
        self.assertEqual(agent.places.libres(), agent.RUNS_MAX)

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
        self.assertEqual(agent.places.libres(), agent.RUNS_MAX)

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


class FileDesPlacesSuivie(FileDesPlaces.__base__):
    """Ce que la file dit d'elle (journal, `GET /api/places`), et ce qui la garde vivante :
    le fil de surveillance, la prolongation d'un pré-run long, la place gardée par la sonde."""

    def setUp(self):
        super().setUp()
        self.addCleanup(FileDesPlaces.vider)

    def tenir(self, n):
        tenus = [agent.Run("bac", "key", cle_du_run=CLE) for _ in range(n)]
        self.addCleanup(lambda: [t.liberer() for t in tenus])
        return tenus

    def test_n_fils_concurrents_jamais_plus_de_max_places(self):
        tenues, pic, servis, verrou = [0], [0], [], threading.Lock()

        def worker():
            r = agent.reserver_place(5, qui="banc")
            if r["place"] is None:
                servis.append(None)
                return
            with verrou:
                tenues[0] += 1
                pic[0] = max(pic[0], tenues[0])
            time.sleep(0.02)
            with verrou:
                tenues[0] -= 1
            servis.append(agent.rendre_place(r["place"])["rendue"])
        fils = [threading.Thread(target=worker) for _ in range(4 * agent.RUNS_MAX)]
        for f in fils:
            f.start()
        for f in fils:
            f.join(10)
        self.assertEqual(servis, [True] * len(fils))      # tous servis, chacun à son tour
        self.assertLessEqual(pic[0], agent.RUNS_MAX)
        self.assertEqual(agent.places.libres(), agent.RUNS_MAX)

    def test_n_fils_sans_rendre_les_surnumeraires_sont_refuses_proprement(self):
        vus, verrou = [], threading.Lock()

        def worker():
            r = agent.reserver_place(0.2)
            with verrou:
                vus.append(r["place"])
        fils = [threading.Thread(target=worker) for _ in range(2 * agent.RUNS_MAX)]
        for f in fils:
            f.start()
        for f in fils:
            f.join(5)
        self.assertEqual(sum(p is not None for p in vus), agent.RUNS_MAX)
        self.assertEqual(vus.count(None), agent.RUNS_MAX)
        self.assertEqual(agent.places.libres(), 0)
        self.assertEqual(len(agent.etat_des_places()["reservees"]), agent.RUNS_MAX)

    def test_le_tour_de_surveillance_survit_a_une_exception(self):
        jeton = agent.reserver_place(0)["place"]
        agent.reservations[jeton].fin = 0          # déjà échue
        vraie, appels = agent.echoir_reservations, iter([RuntimeError("boum")])

        def echoir():
            for exc in appels:
                raise exc
            return vraie()
        with mock.patch.object(agent, "echoir_reservations", side_effect=echoir), \
                self.assertLogs("ferme", "INFO") as journal:
            self.assertIsNone(agent.un_tour_de_surveillance())
            self.assertEqual(agent.un_tour_de_surveillance(), 1)
        self.assertIn("tour en échec", journal.output[0])
        self.assertIn("RuntimeError: boum", journal.output[0])     # la pile, pas qu'un mot
        self.assertIn(jeton[:8], journal.output[-1])
        self.assertEqual(agent.places.libres(), agent.RUNS_MAX)

    def test_le_fil_de_surveillance_reel_ne_meurt_pas(self):
        jeton = agent.reserver_place(0)["place"]
        agent.reservations[jeton].fin = 0
        vraie, tours = agent.echoir_reservations, []

        def echoir():
            tours.append(1)
            if len(tours) == 1:
                raise RuntimeError("boum")
            return vraie()
        arret = threading.Event()
        with mock.patch.object(agent, "echoir_reservations", side_effect=echoir), \
                self.assertLogs("ferme", "INFO"):
            fil = threading.Thread(target=agent.surveiller_reservations, args=(0.01, arret))
            fil.start()
            debut = time.monotonic()
            while jeton in agent.reservations and time.monotonic() - debut < 2:
                time.sleep(0.01)
            self.assertTrue(fil.is_alive())
            arret.set()
            fil.join(1)
        self.assertNotIn(jeton, agent.reservations)
        self.assertGreaterEqual(len(tours), 2)

    def test_un_worker_mort_entre_reserve_et_claim_rend_sa_place_a_l_echeance(self):
        maintenant = time.monotonic()
        jeton = agent.reserver_place(0, qui="worker-1")["place"]
        self.assertEqual(agent.etat_des_places(maintenant)["libres"], agent.RUNS_MAX - 1)
        with self.assertLogs("ferme", "INFO") as journal:
            self.assertEqual(agent.echoir_reservations(maintenant + agent.PLACE_TENUE_S + 1), 1)
        self.assertIn(f"{jeton[:8]} (worker-1)", journal.output[0])
        etat = agent.etat_des_places()
        self.assertEqual((etat["libres"], etat["reservees"], etat["en_run"]),
                         (agent.RUNS_MAX, [], 0))

    def test_un_pre_run_de_plus_de_30_s_prolonge_garde_sa_place(self):
        t0 = time.monotonic()
        jeton = agent.reserver_place(0)["place"]
        for t in (20, 40, 60):                          # le worker bat toutes les 20 s
            self.assertEqual(agent.prolonger_place(jeton, t0 + t),
                             {"prolongee": True, "tenue_s": agent.PLACE_TENUE_S})
            self.assertEqual(agent.echoir_reservations(t0 + t + agent.PLACE_TENUE_S - 1), 0)
        self.tenir(agent.RUNS_MAX - 1)                  # la box est pleine : la place est à lui
        agent.Run("bac", "key", cle_du_run=CLE, place=jeton).liberer()
        # sans prolongation, la même place serait échue à 30 s
        self.assertEqual(agent.prolonger_place(jeton), {"prolongee": False})

    def test_la_prolongation_est_plafonnee(self):
        t0 = time.monotonic()
        jeton = agent.reserver_place(0)["place"]
        with self.assertLogs("ferme", "WARNING"):
            self.assertEqual(agent.prolonger_place(jeton, t0 + agent.PLACE_TENUE_MAX_S + 1),
                             {"prolongee": False})
        self.assertEqual(agent.echoir_reservations(t0 + agent.PLACE_TENUE_S + 1), 1)

    def test_la_sonde_sur_box_pleine_garde_la_place_pour_le_run_du_travail(self):
        self.tenir(agent.RUNS_MAX - 1)
        jeton = agent.reserver_place(0)["place"]        # la dernière place
        sonde = agent.Run("bac", "key", cle_du_run=CLE, place=jeton, garder=True)
        self.assertEqual(agent.places.libres(), 0)
        # pendant la sonde : ni échue, ni rendue, ni reprise ; la prolongation est acquise
        self.assertEqual(agent.echoir_reservations(time.monotonic() + 3600), 0)
        self.assertEqual(agent.rendre_place(jeton), {"rendue": False})
        self.assertTrue(agent.prolonger_place(jeton)["prolongee"])
        self.assertEqual(agent.etat_des_places()["en_run"], agent.RUNS_MAX)
        sonde.liberer()
        self.assertEqual(agent.places.libres(), 0)       # la place n'est pas revenue à la box
        self.assertIn(jeton, agent.reservations)
        agent.Run("bac", "key", cle_du_run=CLE, place=jeton).liberer()   # le vrai run passe
        self.assertEqual(agent.places.libres(), 1)
        self.assertNotIn(jeton, agent.reservations)

    def test_la_sonde_passe_garder_place_par_la_route(self):
        self.tenir(agent.RUNS_MAX - 1)
        jeton = agent.reserver_place(0)["place"]
        h = agent.Handler.__new__(agent.Handler)
        h.headers = {}
        with mock.patch.object(agent, "flux_du_run", return_value=(e for e in ())), \
                mock.patch.object(agent, "resume_du_run", return_value={"ok": True}):
            self.assertEqual(h._run("bac", {"prompt": "ok", "auth": "key", "api_key": CLE,
                                            "place": jeton, "garder_place": True}),
                             {"ok": True})
        self.assertIn(jeton, agent.reservations)         # gardée pour le run du travail
        self.assertEqual(agent.places.libres(), 0)

    def test_get_places_et_qui(self):
        vu = []
        h = agent.Handler.__new__(agent.Handler)
        h.headers = {"Authorization": f"Bearer {agent.JETON}"}
        h._repondre = lambda statut, corps, *a: vu.append((statut, corps))
        h._corps = lambda: {"attente_s": 0, "qui": "oto-runner abonnement\n@1/" + "x" * 80}
        h.path = "/api/places"
        with self.assertLogs("ferme", "INFO") as journal:
            h._router("POST")
        statut, corps = vu.pop()
        self.assertEqual(statut, 200)
        qui = agent.reservations[corps["place"]].qui
        self.assertEqual(len(qui), agent.QUI_MAX)
        self.assertTrue(qui.startswith("oto-runner-abonnement-@1/x"))
        self.assertIn(f"{corps['place'][:8]} octroyée à {qui}", journal.output[0])
        h._router("GET")
        statut, etat = vu.pop()
        self.assertEqual(statut, 200)
        self.assertEqual(etat["max"], agent.RUNS_MAX)
        self.assertEqual(etat["libres"], agent.RUNS_MAX - 1)
        self.assertEqual(etat["en_run"], 0)
        [r] = etat["reservees"]
        self.assertEqual((r["jeton8"], r["qui"]), (corps["place"][:8], qui))
        self.assertLessEqual(r["reste_s"], agent.PLACE_TENUE_S)
        h.path = f"/api/places/{corps['place']}/prolonger"
        h._router("POST")
        self.assertEqual(vu.pop(), (200, {"prolongee": True, "tenue_s": agent.PLACE_TENUE_S}))
        h._router("DELETE")
        self.assertEqual(vu.pop()[0], 405)
        h.path = "/api/places"
        h._corps = lambda: {"attente_s": 0, "qui": 12}
        h._router("POST")
        self.assertEqual(vu.pop()[0], 400)
        h.headers = {}
        h._router("GET")
        self.assertEqual(vu.pop()[0], 401)

    def test_un_worker_ancien_sans_qui_ni_prolonger_marche_comme_avant(self):
        jeton = agent.reserver_place(0)["place"]
        self.assertIsNone(agent.reservations[jeton].qui)
        agent.Run("bac", "key", cle_du_run=CLE, place=jeton).liberer()
        self.assertEqual(agent.places.libres(), agent.RUNS_MAX)
