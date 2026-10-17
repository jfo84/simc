# Logged-player Power Infusion counterfactuals

The goal is to estimate a logged player's performance if they had never received
Power Infusion, while preserving that player's decisions, delays and mistakes.
Changing haste requires simulating all subsequent casts, resources, procs and
cooldown decisions again. Subtracting damage during PI does not answer this question.

This implementation supplies event capture, observation extraction, exact PI
windows, native foreground and off-GCD intention replay, and paired simulation diagnostics. It does **not** infer a player policy,
reconstruct an encounter automatically, or produce a PI-adjusted ranking. All
results have `ranking_eligible: false`, regardless of baseline damage agreement.

## Capture and inspect a pull

Use Python 3.9 or newer. There are no third-party Python dependencies.
Supply a Warcraft Logs client API OAuth access token in `WCL_ACCESS_TOKEN` using
your normal credential mechanism, or pass `--credentials /private/path/client.json`
with a private JSON file containing `client_id` and `client_secret`. The client
credentials flow reads public reports; the access token stays in memory. Secrets
are not saved in captures or manifests.

```sh
python3 util_scripts/pi_counterfactual.py capture REPORT_CODE 1 capture.json
python3 util_scripts/pi_counterfactual.py summarize capture.json PLAYER_ACTOR_ID observation.json
```

Capture requests every event page for the selected fight with resources included.
It preserves raw events, actor metadata, and the report's absolute start time.
Incomplete pagination or a cursor that does not advance is an error. API access
requires a token authorized to read the report. API capture was verified against the public raid and dungeon examples below on
2026-10-16. The API-shaped pagination fixture remains synthetic; recorded API
state excerpts and public UI exports are identified separately.

Summarize extracts combatant information, cast starts and completions, player and
owned-pet damage, and PI applications, refreshes and removals. Times are seconds
relative to the fight start. A refresh extends the active interval until removal;
a buff active at fight end is marked as end-censored. A removal or refresh without
an application is rejected unless fight-start combatant information establishes
that PI was already active. Known initial auras are applied before equal-time
transitions, independent of row order. Missing combatant information is also rejected. The observation retains initial auras,
buff/stack transitions and actor-owned resource snapshots. `resourceActor=1`
belongs to the event source; `resourceActor=2` belongs to the target. Amounts and
resource types retain their raw API units. A snapshot is an observation, not proof
of pre-decision state or a continuous resource trajectory. Raw events remain
available for later reconstruction of targets, mechanics, movement and deaths.
The API's `gameVersion` and `logVersion` fields do not certify an exact game build.

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

## Replay foreground intentions on a separate action timeline

The experimental replay controller executes native SimC actions in recorded order.
It keeps encounter events and cooldowns on simulation time while allowing casts,
GCDs, resources and procs to change when PI is removed. Divergence persists after
PI expires. This is a pair of runs from combat start with the same seed, rather
than a clone of the native event queue at the first PI application. The runner
rejects different intention traces before that application.

```sh
python3 util_scripts/pi_intent_replay.py plan capture.json PLAYER_ACTOR_ID plan.json
python3 util_scripts/pi_intent_replay.py run plan.json player.simc \
  --simc build/simc --output intention-pair --seed 42
```

The current planner has explicit Frost Mage spell bindings. It matches successful
hardcasts to their begin events and records their start as the intention time.
It refuses uncovered spells, missing or canceled cast starts, channel timing,
multiple targets and ambiguous simultaneous casts. Off-GCD inputs are extracted
into a separate world-time stream rather than serialized as foreground casts.
The complete no-PI control contains 183 foreground intentions and six independent inputs and still fails
this preflight: Ice Cold, Ice Barrier, Ray of Frost, a pre-pull Glacial Spike,
canceled starts and target transitions still need reconstruction. The recorded excerpt and blocker
summary are in `tests/fixtures/pi_counterfactual/control/`. No incomplete plan is
silently promoted to a complete replay.

In the reference run, each intention waits until its recorded time and native
availability. The controller measures the remaining pause after the intended
action becomes available. The no-PI run applies that pause after its own native
availability, including for the first intention. Mandatory cooldown waiting is
not copied as hesitation. Every recorded timestamp remains a lower bound, so the
alternate cannot anticipate a recorded intention. Intentions marked `clock=world`
have no copied pause and remain tied to that lower bound. These rules preserve
an explicit timing hypothesis; the residual pauses are not directly observed
human reaction times. Availability is sampled at intervals of at most 50 ms,
shortened for known cooldown deadlines.

Native APL controls are also available directly:

```ini
# Semicolons separate times; colons separate native actions.
actions=observed_sequence,times=0;2;4:frostbolt:frostbolt:frostbolt
# Delays are relative to each intended action's availability.
actions=observed_sequence,delays=0;0.25;0,anchors=0;2;4:frostbolt:frostbolt:frostbolt
```

Use one controller per player with `iterations=1`, `threads=1`, and spell queuing
and one-button mode disabled. An unavailable action fails after `max_wait`
seconds (default 5) of unexplained waiting. In delay mode, known cooldown waiting
does not consume that budget. An optional `fallback=NAME` selects an explicitly
supplied existing APL while an intention is unavailable after its time floor;
it is a behavior assumption that must be validated. The controller never drops
an unavailable intention silently.

The independent stream uses encounter timestamps in both arms:

```ini
class_talents=shimmer:1
actions+=/observed_off_gcd,times=0.5;25:shimmer:shimmer
```

It dispatches through native off-GCD/cast-while-casting execution without taking
over the foreground cast or GCD. Inputs must be instant, non-channel actions with
no GCD, available to the configured character. Native cast-while-casting flags,
stuns, resources, charges and cooldowns still apply. A non-castable input waits for the
cast to finish; a controller's internal idle wait does not count as a cast.
Availability is checked at the cue and at most 50 ms apart afterward. Any wait
beyond `max_wait` seconds from the recorded cue fails explicitly, including a
cooldown wait; the default is five seconds. Reports retain due and actual times,
which expose late inputs. Repeated identical signatures reuse the same native
action. This supports Shimmer overlap; an item with native cast-time or casting
restrictions cannot be forced through them by the planner's spell binding.

Off-GCD cues stay on world time even when foreground actions diverge. That is an
explicit modeling assumption: a trinket aligned by the player to their rotation
may require a different timing policy. A paired run also checks the independent
input trace before PI. The reference must schedule the complete recorded stream.

Reports include each foreground intention's readiness, native availability,
due time, start and residual pause, plus fallback selections. `scheduled` counts
actions selected for execution; a cast can still finish after the encounter ends.
The paired diagnostic includes executed-action counts and damage, manifests and
both native reports. One seeded trajectory does not measure uncertainty. Native
random draws can diverge after PI, and historical mechanics and encounter inputs
still require validation. Every replay remains `ranking_eligible: false`.

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

## Live capture and first control check

On 2026-10-16, complete API pagination returned 234,339 events in 24 pages for
Jeefy's raid pull and 308,654 events in 31 pages for Vae's dungeon run. Their PI
intervals agree with the independently exported UI CSV intervals to the millisecond.
Both groups include Augmentation support, so their reconstruction must retain
those interactions as well as PI. Full captures remain local artifacts; the small
recorded API state excerpt in the fixtures is explicitly incomplete.

A simpler [Kateros control pull](https://www.warcraftlogs.com/reports/64LzFa2vM37CrNDJ?fight=28&type=summary&source=50)
has neither a priest nor Augmentation. Its 89,283 API events in nine pages contain
no PI. A clean historical SimC build at
`90b0d491aac3754db291e7cd7eaffe0d126c12f7` was used for an initial check, with
recorded gear/talents, consumables and raid buffs adjusted to match logged intellect
and secondary ratings. Over 269.314 seconds, the default damage APL predicts
76,215.9 DPS against 59,907.0 observed: 27.22% too high. The candidate baseline is
rejected; no PI estimate is produced.

The profile, reproduction options, hashes, cast-count comparison and remaining
input uncertainties are recorded in
`tests/fixtures/pi_counterfactual/control/baseline-diagnostic.json`. This is a
provisional validation result, not a benchmark or a diagnosis of player skill.
The default APL omits recorded defensive/utility choices and uses the on-use
trinket three times versus one recorded use. The exact historical game build,
food and encounter constraints are not certified, and race comes from the current
public character profile. Matching aggregate stats alone does not validate a
reconstruction or isolate why its damage differs. The historical engine also
warns that Emberwing Feather's stat-penalty proc chance uses an unverified 10%
placeholder, which remains a mechanics uncertainty.

## Acceptance work still required

1. Certify historical patch, remaining consumable inputs, spell IDs and pets for
   the selected recorded pulls. Public API capture and initial profile extraction
   are available; complete input equivalence is not yet established.
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
re-simulation, legacy compatibility and paired-run diagnostics. Native replay checks
cover post-PI divergence, an identical no-PI control, encounter anchors, cooldown
waiting, the first availability-relative delay, explicit fallback and invalid
configuration. Independent-stream checks exercise native overlap during diverging
casts, an identical no-PI control, cast restrictions, cooldown exhaustion and
controller idle waits and native stun events. They do not
establish the accuracy of a logged-player counterfactual.
