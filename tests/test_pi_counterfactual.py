"""Offline importer tests and optional native engine integration checks."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pi_counterfactual", ROOT / "util_scripts/pi_counterfactual.py")
pi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pi)
FIXTURE = ROOT / "tests/fixtures/pi_counterfactual/synthetic.json"
export_spec = importlib.util.spec_from_file_location("pi_log_exports", ROOT / "util_scripts/pi_log_exports.py")
exports = importlib.util.module_from_spec(export_spec)
export_spec.loader.exec_module(exports)



class ImportTests(unittest.TestCase):
    def setUp(self):
        self.bundle = pi.read_json(FIXTURE)

    def test_observed_windows_refresh_and_pet_damage(self):
        observation = pi.summarize(self.bundle, 1)
        self.assertEqual(observation["power_infusion_windows"], [
            {"start": 0, "duration": 5, "end_censored": False},
            {"start": 10, "duration": 3, "end_censored": False},
        ])
        self.assertEqual(observation["observed_damage"], 1200)
        self.assertEqual(observation["owned_actor_ids"], [1, 2])
        self.assertEqual([c["type"] for c in observation["casts"]], ["begincast", "cast"])
        self.assertFalse(observation["ranking_eligible"])

    def test_rejects_missing_page_and_orphan_removal(self):
        self.bundle["pages"].pop()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            pi.summarize(self.bundle, 1)
        self.bundle = pi.read_json(FIXTURE)
        self.bundle["pages"][0]["data"] = [
            e for e in self.bundle["pages"][0]["data"] if e["type"] != "applybuff"
        ]
        with self.assertRaisesRegex(ValueError, "removal lacks"):
            pi.summarize(self.bundle, 1)

    def test_buff_active_at_end_is_censored(self):
        self.bundle["pages"][1]["data"].pop()
        last = pi.summarize(self.bundle, 1)["power_infusion_windows"][-1]
        self.assertEqual(last, {"start": 10, "duration": 20, "end_censored": True})

    def test_missing_combatant_info_is_not_silently_accepted(self):
        self.bundle["pages"][0]["data"].pop(0)
        with self.assertRaisesRegex(ValueError, "combatantinfo"):
            pi.summarize(self.bundle, 1)

    def test_pagination_and_stalled_cursor(self):
        metadata = {"code": "AbCdEfGh12345678", "startTime": 0,
                    "fights": [self.bundle["fight"]], "masterData": {"actors": self.bundle["actors"]}}
        responses = iter([metadata] + [{"events": p} for p in self.bundle["pages"]])
        variables = []
        def request(query, args):
            variables.append(args)
            return next(responses)
        captured = pi.capture("AbCdEfGh12345678", 1, request)
        self.assertEqual(len(captured["pages"]), 2)
        self.assertEqual([v["start"] for v in variables[1:]], [10000, 20000])
        responses = iter([metadata, {"events": {"data": [], "nextPageTimestamp": 10000}}])
        with self.assertRaisesRegex(ValueError, "did not advance"):
            pi.capture("AbCdEfGh12345678", 1, lambda *_: next(responses))

    def test_diagnostic_compares_player_plus_pet_damage_and_constant_buffs(self):
        observation = pi.summarize(self.bundle, 1)
        def report(damage, uptime):
            return {"sim": {"players": [{"name": "probe", "specialization": "Frost Mage",
                    "collected_data": {"dmg": {"mean": 100}, "compound_dmg": {"mean": damage},
                                       "fight_length": {"mean": 30}},
                    "buffs_constant": [{"name": "power_infusion", "uptime": uptime}] if uptime else []}]}}
        result = pi.diagnose(observation, report(1200, 100 * 8 / 30), report(900, 0))
        self.assertEqual(result["model_damage_difference"], 300)
        self.assertEqual(result["baseline_relative_damage_error"], 0)
        self.assertEqual(result["model_without_pi_dps"], 30)

    def test_no_pi_is_a_valid_control(self):
        for page in self.bundle["pages"]:
            page["data"] = [e for e in page["data"] if e.get("abilityGameID") != pi.PI]
        self.assertEqual(pi.summarize(self.bundle, 1)["power_infusion_windows"], [])


class LogExportTests(unittest.TestCase):
    def raid(self):
        directory = FIXTURE.parent / "raid"
        return exports.import_exports(
            "https://www.warcraftlogs.com/reports/yMrk3jBH8txgvFL4?fight=47&source=137",
            "Jeefy", directory / "jeefy.simc", [directory / "pi-events.csv"], [directory / "casts.csv"])

    def test_recorded_raid_windows_and_cancellations(self):
        result = self.raid()
        self.assertEqual(result["power_infusion_windows"], [
            {"start": 4.255, "duration": 15.014}, {"start": 251.011, "duration": 15.035}])
        self.assertEqual(len(result["cast_events"]), 260)
        self.assertEqual(sum(e["canceled"] for e in result["cast_events"]), 14)
        self.assertEqual(result["cast_events"][0]["ability_name"], "Stormkeeper")
        self.assertFalse(result["ranking_eligible"])
        self.assertIn("race", result["data_gaps"][-1])
        self.assertEqual(result["with_pi_options"][-1],
                         "external_buffs.power_infusion_windows=4.255:15.014/251.011:15.035")

    def test_equal_timestamp_casts_are_not_deduplicated(self):
        events = self.raid()["cast_events"]
        duplicates = [e for e in events if e["time_ms"] == 292967 and e["ability_name"] == "Flame Shock"]
        self.assertEqual(len(duplicates), 2)

    def test_recorded_mplus_windows_and_both_cast_pages(self):
        directory = FIXTURE.parent / "mythic-plus"
        result = exports.import_exports(
            "https://www.warcraftlogs.com/reports/m9tCZ7H68cBpJnrK?fight=4&source=1201",
            "Vae", directory / "vae.simc", [directory / "pi-events.csv"],
            [directory / "casts-1.csv", directory / "casts-2.csv"])
        self.assertEqual(result["power_infusion_windows"], [
            {"start": 51.866, "duration": 15.041}, {"start": 310.716, "duration": 15.008},
            {"start": 455.561, "duration": 15.010}, {"start": 620.338, "duration": 15.017},
            {"start": 816.619, "duration": 15.001}])
        self.assertEqual(len(result["cast_events"]), 689)
        self.assertEqual(result["cast_events"][-1]["time_ms"], 938504)
        unknown = [e for e in result["cast_events"] if e["display"]["Source → Target"] == "VaeUnknown"]
        self.assertEqual(len(unknown), 3)
        self.assertFalse(result["ranking_eligible"])

    def test_page_order_and_recipient_must_match(self):
        directory = FIXTURE.parent / "mythic-plus"
        with self.assertRaisesRegex(ValueError, "ordered"):
            exports.read_events([directory / "casts-2.csv", directory / "casts-1.csv"])
        events, _ = exports.read_events([directory / "pi-events.csv"])
        with self.assertRaisesRegex(ValueError, "different recipient"):
            exports.pi_windows(events, "Cyber")

    def test_partial_pi_capture_and_timestamp_errors_are_rejected(self):
        directory = FIXTURE.parent / "raid"
        events, _ = exports.read_events([directory / "pi-events.csv"])
        with self.assertRaisesRegex(ValueError, "missing"):
            exports.pi_windows(events[:-1], "Jeefy")
        for value in ("00:60.000", "0:5", "nan"):
            with self.assertRaisesRegex(ValueError, "timestamp"):
                exports.milliseconds(value)


@unittest.skipUnless(os.environ.get("SIMC_CLI_PATH"), "Set SIMC_CLI_PATH for native engine checks")
class EngineTests(unittest.TestCase):
    def simulate(self, *options):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            command = [os.environ["SIMC_CLI_PATH"], "mage=probe", "level=90", "race=human", "spec=frost",
                       "actions=frostbolt", "fixed_time=1", "max_time=30", "vary_combat_length=0",
                       "iterations=10", "threads=1", "seed=123", "statistics_level=3",
                       f"json={report},version=2.0.0"] + list(options)
            result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(result.returncode, 0, result.stdout[-3000:])
            return pi.read_json(report)["sim"]["players"][0]

    def test_exact_windows_and_full_action_recomputation(self):
        with_pi = self.simulate("external_buffs.power_infusion_windows=0:5/10:3")
        without_pi = self.simulate("external_buffs.power_infusion_windows=0:5/10:3",
                                   "external_buffs.disable_power_infusion=1")
        buff = next(b for b in with_pi["buffs"] if b["name"] == "power_infusion")
        self.assertAlmostEqual(buff["uptime"], 100 * 8 / 30, places=5)
        self.assertEqual(buff["start_count"], 2)
        self.assertFalse(any(b["name"] == "power_infusion" for b in without_pi["buffs"]))
        def casts(player):
            return next(s["num_executes"]["mean"] for s in player["stats"] if s["name"] == "frostbolt")
        self.assertGreater(casts(with_pi), casts(without_pi))

    def test_disabling_pi_blocks_apl_invocation(self):
        player = self.simulate("actions=invoke_external_buff,name=power_infusion,use_pool=0",
                               "actions+=/frostbolt", "external_buffs.disable_power_infusion=1")
        self.assertFalse(any(b["name"] == "power_infusion" for b in player["buffs"]))
        self.assertGreater(player["collected_data"]["dmg"]["mean"], 0)

    def test_windows_suppress_autonomous_pi_refresh(self):
        player = self.simulate("actions=invoke_external_buff,name=power_infusion,use_pool=0",
                               "actions+=/frostbolt", "external_buffs.power_infusion_windows=0:5/10:3")
        buff = next(b for b in player["buffs"] if b["name"] == "power_infusion")
        self.assertEqual(buff["start_count"], 2)
        self.assertAlmostEqual(buff["uptime"], 100 * 8 / 30, places=5)

    def test_priest_self_cast_is_disabled(self):
        talent_line = next(line for line in (ROOT / "profiles/MID2/MID2_Priest_Shadow.simc").read_text().splitlines()
                           if line.startswith("talents="))
        binary = os.environ["SIMC_CLI_PATH"]
        with tempfile.TemporaryDirectory() as directory:
            uptimes = []
            for disabled in (0, 1):
                report = Path(directory) / f"priest-{disabled}.json"
                result = subprocess.run([binary, "priest=probe", "level=90", "spec=shadow", talent_line,
                                         "actions=power_infusion", "actions+=/mind_blast", "iterations=10",
                                         "fixed_time=1", "max_time=30", "vary_combat_length=0", "threads=1",
                                         f"external_buffs.disable_power_infusion={disabled}",
                                         f"json={report},version=2.0.0"],
                                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                self.assertEqual(result.returncode, 0, result.stdout[-3000:])
                player = pi.read_json(report)["sim"]["players"][0]
                uptimes.append(sum(b.get("uptime", 0) for b in player["buffs"] + player["buffs_constant"]
                                   if b["name"] == "power_infusion"))
            self.assertGreater(uptimes[0], 0)
            self.assertEqual(uptimes[1], 0)

    def test_legacy_schedule_unchanged(self):
        player = self.simulate("external_buffs.power_infusion=0")
        self.assertTrue(any(b["name"] == "power_infusion" for b in player["buffs"]))

    def test_invalid_windows_fail(self):
        binary = os.environ["SIMC_CLI_PATH"]
        for window in ("0", "0:0", "0:-1", "-1:2", "nan:2", "0:inf", "1e100:2", "0:5/4:2"):
            with self.subTest(window=window):
                result = subprocess.run([binary, "mage=probe", "spec=frost", "iterations=1",
                                         f"external_buffs.power_infusion_windows={window}"],
                                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                self.assertNotEqual(result.returncode, 0)

    def test_pair_runner_keeps_results_diagnostic_only(self):
        observation = pi.summarize(pi.read_json(FIXTURE), 1)
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "player.simc"
            profile.write_text("mage=probe\nlevel=90\nrace=human\nspec=frost\n"
                               "external_buffs.pool=power_infusion:120/rallying_cry:120\n"
                               "external_buffs.rallying_cry=100\n"
                               "actions=invoke_external_buff,name=rallying_cry\n"
                               "actions+=/frostbolt\n")
            result = pi.run_pair(observation, profile, os.environ["SIMC_CLI_PATH"],
                                 Path(directory) / "run", 10)
            self.assertEqual(result["status"], "diagnostic_only")
            self.assertFalse(result["ranking_eligible"])
            self.assertEqual(len(result["blockers"]), 3)
            for label in ("with_pi", "without_pi"):
                player = pi.read_json(Path(directory) / "run" / (label + ".json"))["sim"]["players"][0]
                self.assertTrue(any(b["name"] == "rallying_cry" and b.get("uptime", 0) > 0
                                    for b in player["buffs"] + player["buffs_constant"]))


if __name__ == "__main__":
    unittest.main()
