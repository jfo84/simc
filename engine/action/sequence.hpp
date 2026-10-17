// ==========================================================================
// Dedmonwakeen's Raid DPS/TPS Simulator.
// Send questions to natehieter@gmail.com
// ==========================================================================

#pragma once

#include "config.hpp"
#include "action/action.hpp"
#include "util/timespan.hpp"
#include <vector>
#include <string>

struct action_t;
struct action_state_t;
struct player_t;

// Sequence =================================================================

struct sequence_t : public action_t
{
  bool waiting;
  int sequence_wait_on_ready;
  std::vector<action_t*> sub_actions;
  int current_action;
  bool restarted;
  timespan_t last_restart;
  bool has_option;

  sequence_t(player_t*, util::string_view sub_action_str);

  void schedule_execute(action_state_t* execute_state = nullptr) override;
  void reset() override;
  bool action_ready() override;
  bool ready() override;
  bool target_ready( player_t* target ) override;
  void init_finished() override;
  void sequence_add_fn( std::string& a_str, std::string& t_str ) const override;

  void restart();
  bool can_restart();
};

struct strict_sequence_t : public action_t
{
  size_t current_action;
  std::vector<action_t*> sub_actions;
  std::string seq_name_str;

  // Allow strict sequence sub-actions to be skipped if they are not ready. Default = false.
  bool allow_skip;

  strict_sequence_t( player_t*, util::string_view options );

  bool ready() override;
  void reset() override;
  void cancel() override;
  void interrupt_action() override;
  void schedule_execute(action_state_t* execute_state = nullptr) override;
  void init_finished() override;
};

// Recorded intentions use either encounter timestamps or delays after native readiness.
struct observed_sequence_t final : public action_t
{
  struct trace_entry_t
  {
    size_t index;
    timespan_t foreground_ready_time, ready_time, due_time, start_time, idle_before;
    std::string action;
    bool fallback;
  };

  std::vector<action_t*> intents;
  std::vector<timespan_t> times, delays, anchors;
  std::vector<trace_entry_t> trace, reported_trace;
  size_t reported_cursor = 0;
  action_t* wait_action;
  action_priority_list_t* fallback_list;
  std::string fallback_name, options_text, time_str, delay_str, anchor_str;
  double max_wait_seconds = 5.0;
  timespan_t max_wait, due_time, foreground_ready_time, ready_time, blocked_since;
  size_t cursor;
  bool pending;

  observed_sequence_t( player_t*, util::string_view );
  void init() override;
  void init_finished() override;
  void reset() override;
  action_t* select_next();
};
