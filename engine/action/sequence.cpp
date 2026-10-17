// ==========================================================================
// Dedmonwakeen's DPS-DPM Simulator.
// Send questions to natehieter@gmail.com
// ==========================================================================

#include "sequence.hpp"

#include "player/player.hpp"
#include "buff/buff.hpp"
#include "player/action_priority_list.hpp"
#include <algorithm>
#include <cmath>
#include <map>
#include "sim/option.hpp"
#include "sim/sim.hpp"
#include "sim/event.hpp"
#include "sim/cooldown.hpp"
#include "util/util.hpp"

// ==========================================================================
// Sequence Action
// ==========================================================================

namespace
{
void parse_actions( player_t* p, std::vector<std::string_view>& splits, std::vector<action_t*>& sub_actions,
                    size_t first = 1 )
{
  for ( auto i = first; i < splits.size(); i++ )
  {
    auto cut_pt = splits[ i ].find( ',' );
    auto action_name = splits[ i ].substr( 0, cut_pt );
    std::string_view action_options;

    if ( cut_pt != std::string_view::npos )
      action_options = splits[ i ].substr( cut_pt + 1 );

    auto a = p->create_action( action_name, action_options );
    if ( !a )
    {
      if ( p->sim->strict_parsing )
      {
        sub_actions.clear();
        return;
      }

      p->sim->error( "{} unknown sequence action '{}', ignoring.", *p, action_name );
      continue;
    }
    else
    {
      a->sequence = true;
      sub_actions.push_back( a );
    }
  }
}
}  // namespace

// sequence_t::sequence_t ===================================================

sequence_t::sequence_t( player_t* p, util::string_view sub_action_str ) :
  action_t( ACTION_SEQUENCE, "default", p ),
  waiting( false ), sequence_wait_on_ready( -1 ),
  current_action( -1 ), restarted( false ), last_restart( timespan_t::min() ), has_option( false )
{
  trigger_gcd = timespan_t::zero();

  auto splits = util::string_split<util::string_view>( sub_action_str, ":" );
  if ( !splits.empty() && splits[ 0 ].find( '=' ) != std::string::npos )
  {
    add_option( opt_string( "name", name_str ) );
    parse_options( splits[ 0 ] );
    has_option = true;
  }

  // Skip first token if it's an option
  parse_actions( p, splits, sub_actions, has_option );

  sequence_wait_on_ready = option.wait_on_ready;
  option.wait_on_ready = -1;
}

// sequence_t::init_finished ================================================

void sequence_t::init_finished()
{
  action_t::init_finished();

  // Clean-up invalid actions
  range::erase_remove( sub_actions, []( action_t* a ) { return a->background; } );

  if ( sub_actions.empty() )
    throw sc_invalid_apl_argument( "Invalid sequence, actions could not be created or no actions found." );
}

// sequence_t::schedule_execute =============================================

void sequence_t::schedule_execute( action_state_t* state )
{
  assert( 0 <= current_action && as<std::size_t>( current_action ) < sub_actions.size() );

  if ( waiting )
  {
    timespan_t available = player->available();

    if ( sim->log )
    {
      sim->print_log( "Player {} is waiting for {:.3f}, since action #{} ({}) is not ready", player->name(),
                      available.total_seconds(), current_action, sub_actions[ current_action ]->name() );
    }

    player->schedule_ready( available, true );
    waiting = false;
    return;
  }

  if ( sim->log )
  {
    sim->print_log( "Player {} executes Schedule {} action #{} '{}'", player->name(), name(), current_action,
                    sub_actions[ current_action ]->name() );
  }

  sub_actions[ current_action++ ]->schedule_execute( state );

  // No longer restarted
  restarted = false;
}

// sequence_t::reset ========================================================

void sequence_t::reset()
{
  action_t::reset();
  /*
    if ( current_action == -1 )
    {
      for ( size_t i = 0; i < sub_actions.size(); ++i )
      {
        if ( sub_actions[ i ] -> wait_on_ready == -1 )
          sub_actions[ i ] -> wait_on_ready = wait_on_ready;
      }
    }
  */
  waiting = false;
  current_action = 0;
  restarted = false;
  last_restart = timespan_t::min();
}

// sequence_t::action_ready =================================================

bool sequence_t::action_ready()
{
  if ( sub_actions.empty() || current_action >= as<int>( sub_actions.size() ) || !action_t::action_ready() )
    return false;

  for ( int num_sub_actions = as<int>( sub_actions.size() ); current_action < num_sub_actions; ++current_action )
  {
    action_t* a = sub_actions[ current_action ];

    if ( a->action_ready() )
      return true;

    // If the sequence is flagged wait_on_ready, we'll wait for available()
    // amount of time, but do not proceed further in the action list, nor
    // in the sequence
    if ( sequence_wait_on_ready == 1 )
    {
      waiting = true;
      return true;
    }
  }

  return false;
}

bool sequence_t::ready()
{
  return !sub_actions.empty() && current_action < as<int>( sub_actions.size() ) && action_t::ready() &&
         sub_actions[ current_action ]->ready();
}

bool sequence_t::target_ready( player_t* t )
{
  return !sub_actions.empty() && current_action < as<int>( sub_actions.size() ) && action_t::target_ready( t ) &&
         sub_actions[ current_action ]->target_ready( t );
}

// sequence_t::sequence_add_fn ==============================================

void sequence_t::sequence_add_fn( std::string& a_str, std::string& t_str ) const
{
  // current_action is advanced in schedule_execute so we need to use current_action - 1
  auto _idx = current_action - 1;
  a_str = fmt::format( "</b>SEQ {}[{}]<br><b>{}", name_str, _idx, sub_actions[ _idx ]->name_str );
  t_str = target->name_str;
}

// ==========================================================================
// Strict Sequence Action
// ==========================================================================

strict_sequence_t::strict_sequence_t( player_t* p, util::string_view options )
  : action_t( ACTION_SEQUENCE, "strict_sequence", p ), current_action( 0 ), allow_skip( false )
{
  trigger_gcd = timespan_t::zero();

  auto splits = util::string_split<util::string_view>( options, ":" );
  if ( ! splits.empty() )
  {
    add_option( opt_bool( "allow_skip", allow_skip ) );
    add_option( opt_string( "name", seq_name_str ) );
    parse_options( splits[ 0 ] );
  }

  parse_actions( p, splits, sub_actions );
}

void strict_sequence_t::init_finished()
{
  action_t::init_finished();

  // Clean-up invalid actions
  range::erase_remove( sub_actions, []( action_t* a ) { return a->background; } );

  if ( sub_actions.empty() )
    throw sc_invalid_apl_argument( "Invalid strict sequence, actions could not be created or no actions found." );
}

void strict_sequence_t::cancel()
{
  action_t::cancel();

  if ( player -> strict_sequence == this )
    player -> strict_sequence = nullptr;

  current_action = 0;
}

void strict_sequence_t::reset()
{
  action_t::reset();

  if ( player -> strict_sequence == this )
    player -> strict_sequence = nullptr;

  current_action = 0;
}

void strict_sequence_t::interrupt_action()
{
  action_t::interrupt_action();

  if ( player -> strict_sequence == this )
    player -> strict_sequence = nullptr;

  current_action = 0;
}

bool strict_sequence_t::ready()
{
  if ( sub_actions.empty() ) return false;
  if ( ! action_t::ready() ) return false;

  // Strict sequences need all actions to be usable before it commits
  if ( ! allow_skip )
  {
    for (auto & elem : sub_actions)
    {
      if ( ! elem -> ready() )
        return false;
    }

    return true;
  }
  else
  { 
    // Ready if at least one action is usable
    return range::any_of( sub_actions, []( action_t* a ) {
        return ! a -> background && a -> ready();
    } );
  }
}

void strict_sequence_t::schedule_execute( action_state_t* state )
{
  assert( ( current_action == 0 && ! player -> strict_sequence ) ||
          ( current_action < sub_actions.size() && player -> strict_sequence ) );
  // Check that we're not discarding the state since we cannot give it to queue_execute.
  (void)state; assert( ! state );

  if ( current_action == 0 )
    player -> strict_sequence = this;

  bool scheduled = false;
  if ( ! allow_skip )
  {
    if ( sim -> log )
      sim -> out_log.print( "{} executes strict_sequence '{}' action #{} \"{}\"",
                     *player, seq_name_str, current_action, sub_actions[ current_action ] -> name() );

    // Sanity check the strict sequence, so that it does not get stuck
    // mid-sequence. People are expected to write strict_sequences that cannot
    // have this happen, but it's bound to be a problem if some buff for example
    // fades between the first, and last cast (and it's needed by the last cast).
    if ( ! sub_actions[ current_action ] -> ready() )
    {
      if ( sim -> debug )
        sim -> out_debug.print( "{} strict_sequence '{}' action #{} \"{}\" is no longer ready, aborting sequence",
          *player, seq_name_str, current_action, sub_actions[ current_action ] -> name() );
      cancel();
      return;
    }

    sub_actions[ current_action++ ] -> queue_execute( execute_type::FOREGROUND );
    scheduled = true;
  }
  else
  {
    // Allow skipping of unusable actions
    while ( current_action < sub_actions.size() &&
            ! sub_actions[ current_action ] -> background &&
            ! sub_actions[ current_action ] -> ready() )
    {
      if ( sim -> debug )
      {
        sim -> out_debug.print( "{} strict_sequence '{}' action #{} \"{}\" is not ready, skipping... ",
          *player, seq_name_str, current_action, sub_actions[ current_action ] -> name() );
      }
      current_action++;
    }

    if ( current_action < sub_actions.size() )
    {
      sim -> print_log( "{} executes strict_sequence '{}' action #{} \"{}\"",
                       *player, seq_name_str, current_action, sub_actions[ current_action ] -> name() );

      player->sequence_add( sub_actions[ current_action ], sub_actions[ current_action ]->target );
      sub_actions[ current_action++ ]->queue_execute( execute_type::FOREGROUND );
      scheduled = true;
    }
  }

  // Strict sequence is over, normal APL commences on the next ready event
  if ( current_action == sub_actions.size() )
  {
    sim -> print_debug( "{} strict_sequence '{}' end reached, strict sequence over",
        *player, seq_name_str );
    player -> strict_sequence = nullptr;
    current_action = 0;

    // If nothing to scheduled, the skippable strict sequence finished and went to the end. Player
    // ready event must be re-kicked so that the actor will not freeze.
    if ( ! scheduled )
    {
      player -> schedule_ready();
    }
  }
}

void sequence_t::restart() { current_action = 0; restarted = true; last_restart = sim->current_time(); }

bool sequence_t::can_restart()
{
  return !restarted && last_restart < sim->current_time();
}


namespace
{
struct observed_wait_t final : public action_t
{
  timespan_t duration = 0_ms;

  explicit observed_wait_t( player_t* p ) : action_t( ACTION_OTHER, "observed_wait", p )
  {
    trigger_gcd = 0_ms;
    harmful = interrupt_auto_attack = false;
    quiet = sequence = true;
    target = p;
  }

  timespan_t execute_time() const override
  {
    return duration;
  }

  void execute() override
  {
    player->iteration_waiting_time += time_to_execute;
  }
};

std::vector<timespan_t> observed_times( std::string_view value )
{
  std::vector<timespan_t> result;
  for ( auto part : util::string_split<std::string_view>( value, ";", false ) )
  {
    auto seconds = util::to_double( part );
    if ( !std::isfinite( seconds ) || seconds < 0 || seconds >= timespan_t::max().total_seconds() )
      throw sc_invalid_apl_argument( "Observed times must be finite, nonnegative seconds." );
    result.push_back( timespan_t::from_seconds( seconds ) );
  }
  return result;
}

std::vector<action_t*> observed_actions( player_t* player, const std::vector<std::string_view>& parts )
{
  std::vector<action_t*> intents;
  std::map<std::string_view, action_t*> unique;
  for ( size_t i = 1; i < parts.size(); ++i )
  {
    auto [ found, inserted ] = unique.emplace( parts[ i ], nullptr );
    if ( inserted )
    {
      auto separator = parts[ i ].find( ',' );
      auto name = parts[ i ].substr( 0, separator );
      auto opts = separator == std::string_view::npos ? std::string_view{} : parts[ i ].substr( separator + 1 );
      found->second = player->create_action( name, opts );
      if ( !found->second || found->second->type == ACTION_SEQUENCE || found->second->type == ACTION_CALL ||
           found->second->type == ACTION_VARIABLE || dynamic_cast<observed_off_gcd_t*>( found->second ) )
        throw sc_invalid_apl_argument( "Observed intents must name executable actions." );
      found->second->sequence = true;
    }
    intents.push_back( found->second );
  }
  return intents;
}
}  // namespace

observed_sequence_t::observed_sequence_t( player_t* p, util::string_view options )
  : action_t( ACTION_SEQUENCE, "observed_sequence", p ), wait_action( nullptr ), fallback_list( nullptr ),
    options_text( options ), max_wait( 5_s ), cursor( 0 ), pending( false )
{
  trigger_gcd = 0_ms;
  harmful = interrupt_auto_attack = false;
  quiet = true;
  target = p;
  add_option( opt_string( "times", time_str ) );
  add_option( opt_string( "delays", delay_str ) );
  add_option( opt_string( "anchors", anchor_str ) );
  add_option( opt_string( "fallback", fallback_name ) );
  add_option( opt_float( "max_wait", max_wait_seconds ) );
}

void observed_sequence_t::init()
{
  auto parts = util::string_split<std::string_view>( options_text, ":", false );
  if ( parts.size() < 2 )
    throw sc_invalid_apl_argument( "Observed sequence requires timing options and actions." );
  parse_options( parts.front() );
  if ( time_str.empty() == delay_str.empty() || !std::isfinite( max_wait_seconds ) || max_wait_seconds < 0 ||
       max_wait_seconds >= timespan_t::max().total_seconds() )
    throw sc_invalid_apl_argument( "Observed sequence requires times or delays and max_wait >= 0." );
  max_wait = timespan_t::from_seconds( max_wait_seconds );
  times = time_str.empty() ? std::vector<timespan_t>{} : observed_times( time_str );
  delays = delay_str.empty() ? std::vector<timespan_t>{} : observed_times( delay_str );
  anchors = anchor_str.empty() ? std::vector<timespan_t>{} : observed_times( anchor_str );
  if ( !std::is_sorted( times.begin(), times.end() ) )
    throw sc_invalid_apl_argument( "Observed encounter times must be ordered." );

  intents = observed_actions( player, parts );
  if ( intents.size() != ( times.empty() ? delays.size() : times.size() ) ||
       ( !anchors.empty() && anchors.size() != intents.size() ) )
    throw sc_invalid_apl_argument( "Observed timings and actions must have the same length." );
  wait_action = new observed_wait_t( player );
  action_t::init();
}

void observed_sequence_t::init_finished()
{
  action_t::init_finished();
  if ( std::count_if( player->action_list.begin(), player->action_list.end(), []( const action_t* a ) {
         return dynamic_cast<const observed_sequence_t*>( a ) != nullptr;
       } ) != 1 || !action_list || action_list->name_str == "precombat" )
    throw sc_invalid_apl_argument( "Use one observed sequence per player, outside precombat." );
  if ( player->enable_spell_queue || player->one_button_mode )
    throw sc_invalid_apl_argument( "Observed sequence requires spell queue and one-button mode disabled." );
  if ( sim->threads != 1 || sim->iterations != 1 )
    throw sc_invalid_apl_argument( "Observed sequence requires threads=1 and iterations=1; repeat independent seed pairs." );
  if ( !fallback_name.empty() )
  {
    fallback_list = player->find_action_priority_list( fallback_name );
    if ( !fallback_list || fallback_list == action_list )
      throw sc_invalid_apl_argument( "Observed fallback must name a separate existing action list." );
  }
  for ( auto* intent : intents )
    if ( intent->background )
      throw sc_invalid_apl_argument( "Observed intent is unavailable in this character's configuration." );
}

void observed_sequence_t::reset()
{
  if ( !trace.empty() )
  {
    reported_trace = trace;
    reported_cursor = cursor;
  }
  action_t::reset();
  cursor = 0;
  pending = false;
  blocked_since = timespan_t::min();
  trace.clear();
}

action_t* observed_sequence_t::select_next()
{
  if ( cursor == intents.size() )
    return nullptr;
  auto now = sim->current_time();
  if ( !pending )
  {
    foreground_ready_time = now;
    ready_time = timespan_t::min();
    due_time = times.empty() ? timespan_t::min() : times[ cursor ];
    pending = true;
    blocked_since = timespan_t::min();
  }
  auto* wait = static_cast<observed_wait_t*>( wait_action );
  auto* intent = intents[ cursor ];
  auto available = intent->action_ready() && intent->cooldown->queue_delay() <= 0_ms;
  auto poll = 50_ms;
  for ( auto* cooldown : { intent->cooldown, intent->internal_cooldown, intent->line_cooldown.get() } )
    if ( cooldown->remains() > 0_ms )
      poll = std::min( poll, cooldown->remains() );
  if ( !available )
  {
    ready_time = timespan_t::min();
    if ( times.empty() )
      due_time = timespan_t::min();
    auto floor = times.empty() ? ( anchors.empty() ? 0_ms : anchors[ cursor ] ) : times[ cursor ];
    if ( now < floor )
    {
      wait->duration = std::min( poll, floor - now );
      return wait;
    }
    // In the alternate branch a native cooldown is mandatory waiting, not player hesitation.
    auto mandatory = times.empty() && std::max( { intent->cooldown->remains(),
                         intent->internal_cooldown->remains(), intent->line_cooldown->remains() } ) > 0_ms;
    if ( mandatory )
      blocked_since = timespan_t::min();
    else if ( blocked_since == timespan_t::min() )
      blocked_since = now;
    if ( !mandatory && now - blocked_since >= max_wait )
      throw sc_runtime_error( fmt::format( "Observed intent {} ({}) remained unavailable for {:.3f}s.",
                                          cursor, intent->name(), max_wait.total_seconds() ) );
    if ( fallback_list )
    {
      auto* fallback = player->select_action( *fallback_list, execute_type::FOREGROUND );
      if ( fallback )
      {
        trace.push_back( { cursor, foreground_ready_time, ready_time, due_time, now, 0_ms, fallback->name_str, true } );
        return fallback;
      }
    }
    wait->duration = mandatory ? poll : std::min( poll, max_wait - ( now - blocked_since ) );
    return wait;
  }
  blocked_since = timespan_t::min();
  if ( ready_time == timespan_t::min() )
  {
    ready_time = now;
    if ( times.empty() )
      due_time = now + delays[ cursor ];
    if ( !anchors.empty() )
      due_time = std::max( due_time, anchors[ cursor ] );
  }
  if ( now < due_time )
  {
    wait->duration = std::min( poll, due_time - now );
    return wait;
  }
  trace.push_back( { cursor, foreground_ready_time, ready_time, due_time, now,
                    std::max( 0_ms, due_time - ready_time ), intent->name_str, false } );
  ++cursor;
  pending = false;
  return intent;
}

observed_off_gcd_t::observed_off_gcd_t( player_t* p, util::string_view options )
  : action_t( ACTION_OTHER, "observed_off_gcd", p ), options_text( options )
{
  background = quiet = true;
  harmful = interrupt_auto_attack = false;
  trigger_gcd = 0_ms;
  target = p;
  add_option( opt_string( "times", time_str ) );
  add_option( opt_float( "max_wait", max_wait_seconds ) );
}

void observed_off_gcd_t::init()
{
  auto parts = util::string_split<std::string_view>( options_text, ":", false );
  if ( parts.size() < 2 )
    throw sc_invalid_apl_argument( "Observed off-GCD stream requires times and actions." );
  parse_options( parts.front() );
  times = observed_times( time_str );
  if ( !std::is_sorted( times.begin(), times.end() ) || !std::isfinite( max_wait_seconds ) ||
       max_wait_seconds < 0 || max_wait_seconds >= timespan_t::max().total_seconds() )
    throw sc_invalid_apl_argument( "Observed off-GCD times must be ordered and max_wait finite and nonnegative." );
  intents = observed_actions( player, parts );
  if ( times.size() != intents.size() )
    throw sc_invalid_apl_argument( "Observed off-GCD times and actions must have the same length." );
  action_t::init();
}

void observed_off_gcd_t::init_finished()
{
  action_t::init_finished();
  if ( std::count_if( player->action_list.begin(), player->action_list.end(), []( const action_t* a ) {
         return dynamic_cast<const observed_off_gcd_t*>( a ) != nullptr;
       } ) != 1 || !action_list || action_list->name_str == "precombat" )
    throw sc_invalid_apl_argument( "Use one observed off-GCD stream per player, outside precombat." );
  if ( sim->threads != 1 || sim->iterations != 1 || player->enable_spell_queue || player->one_button_mode )
    throw sc_invalid_apl_argument( "Observed off-GCD stream requires one iteration/thread and no spell queue or one-button mode." );
  for ( auto* intent : intents )
    if ( intent->background || intent->gcd() != 0_ms || intent->execute_time() != 0_ms || intent->channeled )
      throw sc_invalid_apl_argument( "Observed off-GCD inputs must be available instant actions with no GCD." );
  player->register_combat_begin( [ this ]( player_t* ) { schedule_next(); } );
}

void observed_off_gcd_t::reset()
{
  if ( !trace.empty() )
    reported_trace = trace;
  action_t::reset();
  trace.clear();
  cursor = 0;
}

void observed_off_gcd_t::schedule_next()
{
  if ( cursor < intents.size() )
    make_event( *sim, std::max( 0_ms, times[ cursor ] - sim->current_time() ), [ this ] { dispatch(); } );
}

void observed_off_gcd_t::dispatch()
{
  if ( player->is_sleeping() )
    return;
  auto* intent = intents[ cursor ];
  auto* overlapping = player->executing ? player->executing : player->channeling;
  if ( dynamic_cast<observed_wait_t*>( overlapping ) )
    overlapping = nullptr;
  if ( intent->gcd() != 0_ms || intent->execute_time() != 0_ms || intent->channeled )
    throw sc_runtime_error( "Observed off-GCD input acquired a GCD, cast time or channel." );
  if ( player->resource_regeneration == regen_type::DYNAMIC )
    player->do_dynamic_regen();
  if ( player->buffs.stunned->check() || ( overlapping && !intent->usable_while_casting ) ||
       !intent->action_ready() ||
       intent->cooldown->queue_delay() > 0_ms )
  {
    auto remaining = timespan_t::from_seconds( max_wait_seconds ) - ( sim->current_time() - times[ cursor ] );
    if ( remaining <= 0_ms )
      throw sc_runtime_error( fmt::format( "Observed off-GCD input {} ({}) remained unavailable for {:.3f}s.",
                                          cursor, intent->name(), max_wait_seconds ) );
    make_event( *sim, std::min( 50_ms, remaining ), [ this ] { dispatch(); } );
    return;
  }
  trace.push_back( { cursor, times[ cursor ], sim->current_time(), intent->name_str,
                    overlapping ? overlapping->name_str : "" } );
  intent->queue_execute( overlapping ? execute_type::CAST_WHILE_CASTING : execute_type::OFF_GCD );
  ++cursor;
  schedule_next();
}
