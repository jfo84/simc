# Logged-player Power Infusion counterfactuals

The goal is to estimate a logged player's performance if they had never received
Power Infusion, while preserving that player's decisions, delays and mistakes.
Changing haste requires simulating all subsequent casts, resources, procs and
cooldown decisions again. Subtracting damage during PI does not answer this question.

This implementation supplies event capture, observation extraction, exact PI
windows, and paired simulation diagnostics. It does **not** infer a player policy,
reconstruct an encounter automatically, or produce a PI-adjusted ranking. All
results have `ranking_eligible: false`, regardless of baseline damage agreement.

## Capture and inspect a pull

Use Python 3.9 or newer. There are no third-party Python dependencies.
Supply a Warcraft Logs client API OAuth access token in `WCL_ACCESS_TOKEN` using
your normal credential mechanism; tokens are not saved in captures or manifests.

```sh
python3 util_scripts/pi_counterfactual.py capture REPORT_CODE 1 capture.json
python3 util_scripts/pi_counterfactual.py summarize capture.json PLAYER_ACTOR_ID observation.json
```

Capture requests every event page for the selected fight with resources included.
It preserves raw events, actor metadata, and the report's absolute start time.
Incomplete pagination or a cursor that does not advance is an error. API access
requires a token authorized to read the report. Live API compatibility still needs
verification against a real report; the API-shaped fixture is explicitly synthetic.
Recorded public UI exports are included separately below.

Summarize extracts combatant information, cast starts and completions, player and
owned-pet damage, and PI applications, refreshes and removals. Times are seconds
relative to the fight start. A refresh extends the active interval until removal;
a buff active at fight end is marked as end-censored. A removal or refresh without
an application is rejected because the starting state is unknown. Missing
combatant information is also rejected. Raw events remain available for later
reconstruction of targets, mechanics, other buffs, movement and deaths.

## Import public UI exports without an API token

The public report Summary has **Export to SimC** for the logged gear and talents.
The Buffs and Casts event tables have **CSV** export controls. Select one recipient
and Power Infusion in Buffs, then export its apply/remove events. Export Casts
separately and follow **Next Page** until the final page. Preserve duplicate rows:
not every logged cast is an independent foreground player decision.

```sh
python3 util_scripts/pi_log_exports.py \
  --report-url 'https://www.warcraftlogs.com/reports/yMrk3jBH8txgvFL4?fight=47&source=137' \
  --player Jeefy --profile tests/fixtures/pi_counterfactual/raid/jeefy.simc \
  --pi-events tests/fixtures/pi_counterfactual/raid/pi-events.csv \
  --casts tests/fixtures/pi_counterfactual/raid/casts.csv --output raid-evidence.json
```

This creates a separate `simc.pi.log_exports.v1` evidence bundle containing the
observed PI windows, raw display rows, normalized ability names, canceled casts,
profile and source hashes, and engine options for the intervention. It cannot be
passed to the paired runner as a complete observation: UI CSV exports omit spell
IDs, full resources/buff state, exact fight duration and proof of complete
pagination. The character export also omits race. Those gaps remain explicit,
and the bundle is never ranking-eligible.

Two public examples captured on 2026-10-16 are checked in:

- [Jeefy, Elemental Shaman, Mythic Vorasius](https://www.warcraftlogs.com/reports/yMrk3jBH8txgvFL4?fight=47&type=summary&source=137):
  two PI windows and 260 cast/begin-cast rows, including 14 canceled casts.
- [Vae, Unholy Death Knight, Nexus-Point Xenas +12](https://www.warcraftlogs.com/reports/m9tCZ7H68cBpJnrK?fight=4&type=summary&source=1201):
  five PI windows and 689 rows across two Casts export pages.

Source URLs, capture details and limits are in
`tests/fixtures/pi_counterfactual/provenance.json`. Profiles were copied through
the Summary export control; line breaks were restored after transferring the
clipboard text through the native browser address field. Their export timestamp
is not the combat timestamp. These are recorded data fixtures for reconstruction
work, not evidence that a no-PI player model has been validated.

## Run a candidate behavior model

Provide a SimC profile containing one non-priest player and an explicitly authored
candidate behavior/encounter model, with matching gear, talents and game data.
The profile is not generated from the log. Supplying an ordinary default APL runs
that APL; it does not turn it into the logged player's policy. Profiles and their
includes must be trusted SimC inputs.

```sh
cmake -S . -B build -G Ninja -DBUILD_GUI=OFF -DSC_NO_NETWORKING=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build --target simc -j 2
python3 util_scripts/pi_counterfactual.py run observation.json player.simc \
  --simc build/simc --output diagnostic-run --iterations 1000
```

The two simulations have fixed recorded duration. One receives the observed PI
windows; the other disables PI. Both execute the candidate policy normally, so
casts and all downstream state can diverge. The runner clears legacy external PI
times and checks that reported PI uptime matches the intervention. Explicit PI
windows suppress autonomous PI invocation; other external buff pools remain
available. An observation with no PI disables PI in both arms. Priests are excluded from this runner because replacing their actual casts
with externally scheduled buff applications does not preserve cast costs.

The output directory contains both SimC reports and logs, the observation,
commands, binary and top-level profile hashes, and `diagnostic.json`. Included
profile files are not hashed; keep the full profile tree and binary with the run.
The diagnostic compares damage and spell execution counts and reports the model's
no-PI prediction. Those counts can include modeled background actions and are not
a cast-by-cast fidelity metric. A shared random seed makes each arm repeatable;
it does not preserve the same per-action random outcomes when the rotations diverge.

## Engine controls

These player options are available independently of the Python harness:

```ini
# Seconds relative to combat start; ordered, non-overlapping start:duration pairs.
external_buffs.power_infusion_windows=0:5/120:18

# Suppress timed, invoked and priest self-cast PI for this player.
external_buffs.disable_power_infusion=1
```

Explicit windows cannot be combined with `external_buffs.power_infusion` start
times. Explicit windows suppress PI action selection and invocation so another
source cannot refresh or extend the recorded windows. The original start-time option retains its existing behavior. The disable
option takes precedence over schedules and prevents priest PI action selection
and generic external PI invocation. It affects this player's PI, not raid-wide
priest targeting or other haste effects.

## Acceptance work still required

1. Select a real pull, spec and matching character profile. Verify the API capture,
   spell IDs, pets, game patch and gear/talent reconstruction against that pull.
2. Reconstruct encounter constraints and fit player decision policies using a
   training interval. Preserve mistakes without copying impossible timestamps.
3. Evaluate withheld cast sequences, timing, resources, damage windows and no-PI
   control pulls. Similar total damage alone cannot validate a policy.
4. Run multiple plausible policies and stochastic trials. Report sampling and
   policy uncertainty separately; reject unstable adjusted rankings.
5. Decide whether to hold encounter timing fixed or simulate the raid's changed
   kill time, target deaths and phases. This prototype uses the fixed timeline.

No acceptance threshold is invented here. Thresholds need measured validation on
real pulls before they can support a public ranking.

## Tests

```sh
ctest --test-dir build -R '^PI_Counterfactual$' --output-on-failure
```

The tests exercise offline pagination and event parsing, native PI window timing,
disabling PI through scheduled and APL sources, changed cast counts after a full
re-simulation, legacy compatibility and paired-run diagnostics. They do not
establish the accuracy of a logged-player counterfactual.
