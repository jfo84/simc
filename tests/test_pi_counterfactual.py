"""Offline importer tests and optional native engine integration checks."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pi_counterfactual", ROOT / "util_scripts/pi_counterfactual.py")
pi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pi)
FIXTURE = ROOT / "tests/fixtures/pi_counterfactual/synthetic.json"
export_spec = importlib.util.spec_from_file_location("pi_log_exports", ROOT / "util_scripts/pi_log_exports.py")
exports = importlib.util.module_from_spec(export_spec)
export_spec.loader.exec_module(exports)
sys.path.insert(0, str(ROOT / "util_scripts"))
import pi_intent_replay as replay




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

    def test_recorded_resource_snapshots_belong_to_the_selected_actor(self):
        fixture = pi.read_json(FIXTURE.parent / "raid/api-state-excerpt.json")
        result = pi.actor_state_observations(fixture["events"], fixture["actor"], fixture["fight_start"])
        self.assertEqual(len(result["resource_snapshots"]), 2)
        self.assertEqual([s["resources"][0]["type"] for s in result["resource_snapshots"]], [11, 11])
        self.assertEqual([s["resources"][0]["amount"] for s in result["resource_snapshots"]], [0, 10])
        self.assertEqual([b["stacks"] for b in result["buff_events"] if b["stacks"] is not None], [4, 3])

    def test_target_resources_are_not_cast_resources(self):
        cast = next(e for e in self.bundle["pages"][0]["data"] if e["type"] == "cast")
        cast.update(resourceActor=2, classResources=[{"type": 11, "amount": 99, "max": 150}])
        observation = pi.summarize(self.bundle, 1)
        self.assertEqual(next(c for c in observation["casts"] if c["type"] == "cast")["resources"], [])
        self.assertEqual(observation["resource_snapshots"], [])

    def test_initial_pi_aura_supplies_the_known_starting_state(self):
        self.bundle["pages"][0]["data"][0]["auras"] = [{"ability": pi.PI, "stacks": 1}]
        self.bundle["pages"][0]["data"] = [
            e for e in self.bundle["pages"][0]["data"] if e["type"] != "applybuff"]
        observation = pi.summarize(self.bundle, 1)
        self.assertEqual(observation["power_infusion_windows"][0],
                         {"start": 0, "duration": 5, "end_censored": False})
        self.assertEqual(observation["initial_auras"], [{"ability": pi.PI, "stacks": 1}])

    def test_initial_aura_precedes_equal_time_transitions_regardless_of_row_order(self):
        events = self.bundle["pages"][0]["data"]
        events[0]["auras"] = [{"ability": pi.PI, "stacks": 1}]
        removal = dict(next(e for e in events if e["type"] == "removebuff"), timestamp=10000)
        self.bundle["pages"][0]["data"] = [removal] + [
            e for e in events if e["type"] not in {"applybuff", "removebuff"}]
        result = pi.summarize(self.bundle, 1)
        self.assertEqual(result["power_infusion_windows"],
                         [{"start": 10, "duration": 3, "end_censored": False}])

    def test_client_credentials_authenticate_without_returning_the_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.json"
            path.write_text(json.dumps({"client_id": "fixture-client", "client_secret": "fixture-secret"}))
            with patch.object(pi.urllib.request, "urlopen", return_value=io.BytesIO(b'{"access_token":"fixture-token"}')) as request:
                token = pi.client_access_token(path)
            self.assertEqual(token, "fixture-token")
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, "https://www.warcraftlogs.com/oauth/token")
            self.assertEqual(sent.data, b"grant_type=client_credentials")
            self.assertNotIn("fixture-secret", sent.full_url)
            with patch.object(pi.urllib.request, "urlopen", return_value=io.BytesIO(b'{}')):
                with self.assertRaisesRegex(ValueError, "missing an access token"):
                    pi.client_access_token(path)

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


class IntentionPlanTests(unittest.TestCase):
    def test_recorded_excerpt_preserves_starts_and_reports_missing_mechanics(self):
        fixture = pi.read_json(FIXTURE.parent / "control/api-intents-excerpt.json")
        result = replay.extract_intentions(fixture["events"], fixture["start"], fixture["actor"])
        frostbolt = next(i for i in result["intents"] if i["action"] == "frostbolt")
        self.assertEqual(frostbolt["time"], 13.311)
        self.assertEqual(frostbolt["observed_completion"], 14.444)
        blockers = {b["kind"]: b for b in result["blockers"]}
        self.assertEqual(blockers["unsupported_actions"]["counts"], {"Ice Cold": 1, "Ice Barrier": 1})
        self.assertEqual(blockers["channel_timing"]["actions"], ["Ray of Frost"])
        self.assertEqual([i["action"] for i in result["off_gcd_intents"]],
                         ["shimmer", "use_item,name=emberwing_feather"])
        self.assertFalse(any(i["action"] == "shimmer" for i in result["intents"]))

    def test_missing_hard_cast_start_is_not_treated_as_an_instant(self):
        event = {"type": "cast", "timestamp": 1000, "sourceID": 1, "targetID": 2,
                 "ability": {"guid": 199786, "name": "Glacial Spike"}}
        result = replay.extract_intentions([event], 0, 1)
        self.assertEqual(result["blockers"][0]["kind"], "missing_cast_starts")

    def test_sequence_delimiters_and_world_anchors(self):
        plan = {"intents": [{"time": 1, "action": "frostbolt"},
                            {"time": 40, "action": "frost_nova", "clock": "world"}]}
        self.assertEqual(replay.sequence_option(plan, [1, 0], "delays"),
                         "actions=observed_sequence,delays=1.000;0.000,anchors=1.000;40.000:frostbolt:frost_nova")
        plan["intents"][0]["action"] = "frostbolt/frostbolt"
        with self.assertRaisesRegex(ValueError, "delimiters"):
            replay.sequence_option(plan, [1, 0], "delays")


@unittest.skipUnless(os.environ.get("SIMC_CLI_PATH"), "Set SIMC_CLI_PATH for native replay checks")
class IntentionReplayTests(unittest.TestCase):
    def pair(self, directory, plan, extra_profile=""):
        profile = Path(directory) / "probe.simc"
        profile.write_text("mage=probe\nlevel=90\nrace=human\nspec=frost\noptimal_raid=0\n"
                           "actions.precombat=snapshot_stats\n" + extra_profile)
        output = Path(directory) / "pair"
        result = replay.run_pair(plan, profile, os.environ["SIMC_CLI_PATH"], output)
        players = [pi.read_json(output / (name + ".json"))["sim"]["players"][0]
                   for name in ("reference", "without_pi")]
        return result, players

    def plan(self, windows):
        return {"schema": "simc.pi.intent_plan.v1", "duration": 12.3, "blockers": [],
                "power_infusion_windows": windows,
                "intents": [{"time": t, "action": "frostbolt"} for t in [0, 2, 4, 6, 8, 10]]}

    def test_actor_time_expands_and_stays_separate_after_pi_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            result, (reference, alternate) = self.pair(directory, self.plan([{"start": 1, "duration": 6}]))
        ref = reference["observed_sequence"]["trace"]
        alt = alternate["observed_sequence"]["trace"]
        self.assertEqual(ref[0], alt[0])
        self.assertEqual(result["reference_max_lateness"], 0)
        self.assertGreater(alt[2]["start_time"], ref[2]["start_time"])
        self.assertGreater(alt[4]["start_time"], ref[4]["start_time"])
        self.assertGreater(ref[4]["start_time"], 7)
        self.assertEqual(result["reference_native_executes"]["frostbolt"], 6)
        self.assertEqual(result["without_pi_native_executes"]["frostbolt"], 5)
        self.assertGreater(result["reference_damage"], result["without_pi_damage"])
        self.assertFalse(result["ranking_eligible"])

    def test_no_pi_control_replays_identically_on_the_two_clocks(self):
        with tempfile.TemporaryDirectory() as directory:
            result, (reference, alternate) = self.pair(directory, self.plan([]))
        self.assertEqual(result["reference_damage"], result["without_pi_damage"])
        self.assertEqual([e["start_time"] for e in reference["observed_sequence"]["trace"]],
                         [e["start_time"] for e in alternate["observed_sequence"]["trace"]])

    def test_independent_inputs_keep_world_time_during_diverging_casts(self):
        plan = self.plan([{"start": 1, "duration": 6}])
        plan["off_gcd_intents"] = [{"time": t, "action": "shimmer"} for t in (5,)]
        with tempfile.TemporaryDirectory() as directory:
            result, players = self.pair(directory, plan, "class_talents=shimmer:1\n")
        for player in players:
            stream = player["observed_off_gcd"]
            self.assertEqual(stream["scheduled"], 1)
            self.assertEqual([e["start_time"] for e in stream["trace"]], [5])
            self.assertEqual([e["overlapping_action"] for e in stream["trace"]], ["frostbolt"])
        self.assertGreater(players[1]["observed_sequence"]["trace"][2]["start_time"],
                           players[0]["observed_sequence"]["trace"][2]["start_time"])
        self.assertEqual(result["reference_native_executes"]["shimmer"], 1)
        self.assertEqual(result["without_pi_native_executes"]["shimmer"], 1)

    def test_no_pi_control_with_independent_inputs_is_identical(self):
        plan = self.plan([])
        plan["off_gcd_intents"] = [{"time": 0.5, "action": "shimmer"}]
        with tempfile.TemporaryDirectory() as directory:
            result, players = self.pair(directory, plan, "class_talents=shimmer:1\n")
        self.assertEqual(result["reference_damage"], result["without_pi_damage"])
        self.assertEqual(players[0]["observed_off_gcd"], players[1]["observed_off_gcd"])

    def test_off_gcd_native_restrictions_and_invalid_actions_fail_explicitly(self):
        common = [os.environ["SIMC_CLI_PATH"], "mage=probe", "level=90", "race=troll", "spec=frost",
                  "optimal_raid=0", "iterations=1", "threads=1", "enable_spell_queue=0",
                  "fixed_time=1", "max_time=6", "vary_combat_length=0",
                  "actions=observed_sequence,times=0:frostbolt"]
        cases = [("times=0.5,max_wait=0:berserking", 50, "remained unavailable"),
                 ("times=0.5:frostbolt", 30, "instant actions with no GCD"),
                 ("times=0.5;1:berserking", 30, "same length"),
                 ("times=1;0:berserking:berserking", 30, "must be ordered"),
                 ("times=0.5:shimmer", 30, "instant actions with no GCD")]
        for option, status, message in cases:
            with self.subTest(option=option):
                result = subprocess.run(common + ["actions+=/observed_off_gcd," + option],
                                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
                self.assertEqual(result.returncode, status, result.stdout[-2000:])
                self.assertIn(message, result.stdout)

    def test_foreground_controller_wait_is_not_a_cast_restriction(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "wait.json"
            result = subprocess.run(
                [os.environ["SIMC_CLI_PATH"], "mage=probe", "level=90", "race=troll", "spec=frost",
                 "optimal_raid=0", "iterations=1", "threads=1", "enable_spell_queue=0",
                 "fixed_time=1", "max_time=8", "vary_combat_length=0",
                 "actions=observed_sequence,times=0;6:frostbolt:frostbolt",
                 "actions+=/observed_off_gcd,times=2.25,max_wait=0:berserking",
                 f"json={output},version=2.0.0"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout[-2000:])
            event = pi.read_json(output)["sim"]["players"][0]["observed_off_gcd"]["trace"][0]
        self.assertEqual(event["start_time"], 2.25)
        self.assertEqual(event["overlapping_action"], "")

    def test_independent_input_waits_for_native_stun_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "stun.json"
            common = [os.environ["SIMC_CLI_PATH"], "mage=probe", "level=90", "spec=frost",
                      "class_talents=shimmer:1", "optimal_raid=0", "iterations=1", "threads=1",
                      "enable_spell_queue=0", "fixed_time=1", "max_time=8", "vary_combat_length=0",
                      "raid_events=stun,first=1,duration=2,cooldown=100",
                      "actions=observed_sequence,times=0;6:frostbolt:frostbolt"]
            result = subprocess.run(common + ["actions+=/observed_off_gcd,times=1.5:shimmer",
                                               f"json={output},version=2.0.0"],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout[-2000:])
            event = pi.read_json(output)["sim"]["players"][0]["observed_off_gcd"]["trace"][0]
            self.assertGreaterEqual(event["start_time"], 3)
            self.assertLessEqual(event["start_time"], 3.05)
            failed = subprocess.run(common + ["actions+=/observed_off_gcd,times=1.5,max_wait=0:shimmer"],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
            self.assertEqual(failed.returncode, 50, failed.stdout[-2000:])

    def test_off_gcd_repeated_inputs_use_native_charges_and_cooldowns(self):
        plan = self.plan([])
        plan["duration"] = 30
        plan["off_gcd_intents"] = [{"time": t, "action": "shimmer"} for t in (0.5, 0.6, 0.7)]
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "probe.simc"
            profile.write_text("mage=probe\nlevel=90\nspec=frost\nclass_talents=shimmer:1\n"
                               "actions.precombat=snapshot_stats\n")
            with self.assertRaisesRegex(ValueError, "reference replay failed"):
                replay.run_pair(plan, profile, os.environ["SIMC_CLI_PATH"], Path(directory) / "pair")
            log = (Path(directory) / "pair/reference.log").read_text()
            self.assertIn("Observed off-GCD input 1 (shimmer) remained unavailable", log)

    def test_world_anchor_and_cooldown_do_not_expand_with_player_time(self):
        plan = self.plan([{"start": 1, "duration": 6}])
        plan["duration"] = 42
        plan["intents"][0]["action"] = "frost_nova"
        plan["intents"][-1] = {"time": 40, "action": "frost_nova", "clock": "world"}
        with tempfile.TemporaryDirectory() as directory:
            _, players = self.pair(directory, plan)
        for player in players:
            self.assertEqual(player["observed_sequence"]["trace"][-1]["start_time"], 40)

    def test_native_cooldown_wait_is_not_reapplied_as_player_delay(self):
        plan = {"schema": "simc.pi.intent_plan.v1", "duration": 34, "blockers": [],
                "power_infusion_windows": [{"start": 1, "duration": 6}],
                "intents": [{"time": 0, "action": "frost_nova"}, {"time": 30, "action": "frost_nova"}]}
        with tempfile.TemporaryDirectory() as directory:
            result, players = self.pair(directory, plan)
        self.assertAlmostEqual(result["delays"][1], 0, places=3)
        for player in players:
            self.assertAlmostEqual(player["observed_sequence"]["trace"][1]["start_time"], 30, places=3)

    def test_first_delay_is_relative_to_initial_action_availability(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            for mode, timing in (("times", 3), ("delays", 2)):
                output = Path(directory) / (mode + ".json")
                result = subprocess.run(
                    [os.environ["SIMC_CLI_PATH"], "mage=probe", "level=90", "race=human", "spec=frost",
                     "optimal_raid=0", "iterations=1", "threads=1", "fixed_time=1", "max_time=6",
                     "vary_combat_length=0", "enable_spell_queue=0",
                     f"actions=observed_sequence,{mode}={timing}:frostbolt,if=time>=1",
                     f"json={output},version=2.0.0"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout[-2000:])
                events.append(pi.read_json(output)["sim"]["players"][0]["observed_sequence"]["trace"][0])
        for event in events:
            self.assertAlmostEqual(event["ready_time"], 1, places=3)
            self.assertAlmostEqual(event["start_time"], 3, places=3)

    def test_unavailable_intents_fail_explicitly_or_use_the_named_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "probe.simc"
            profile.write_text("mage=probe\nlevel=90\nrace=human\nspec=frost\noptimal_raid=0\n"
                               "actions.fallback=frostbolt\n")
            base = [os.environ["SIMC_CLI_PATH"], str(profile), "iterations=1", "threads=1",
                    "fixed_time=1", "max_time=35", "vary_combat_length=0"]
            failed = subprocess.run(base + ["actions=observed_sequence,times=0;2,max_wait=0:frost_nova:frost_nova"],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
            self.assertEqual(failed.returncode, 50)
            self.assertIn("remained unavailable", failed.stdout)
            output = Path(directory) / "fallback.json"
            succeeded = subprocess.run(base + ["actions=observed_sequence,times=0;2,fallback=fallback,max_wait=40:frost_nova:frost_nova",
                                                f"json={output},version=2.0.0"],
                                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
            self.assertEqual(succeeded.returncode, 0, succeeded.stdout[-2000:])
            trace = pi.read_json(output)["sim"]["players"][0]["observed_sequence"]["trace"]
            self.assertTrue(any(e["fallback"] for e in trace))
            self.assertEqual([e["index"] for e in trace if not e["fallback"]], [0, 1])

    def test_invalid_replay_configuration_fails_cleanly(self):
        for option in ("times=0;1:frostbolt", "times=1;0:frostbolt:frostbolt", "delays=-1:frostbolt",
                       "times=0,max_wait=nan:frostbolt", "times=0,delays=0:frostbolt"):
            with self.subTest(option=option):
                result = subprocess.run([os.environ["SIMC_CLI_PATH"], "mage=probe", "spec=frost",
                                         "iterations=1", "threads=1", "actions=observed_sequence," + option],
                                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
                self.assertEqual(result.returncode, 30, result.stdout[-1000:])


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
