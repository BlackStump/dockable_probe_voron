# Dockable Probe
# This provides support for probes that are magnetically coupled
# to the toolhead and stowed in a dock when not in use.
#
# Original: Copyright (C) 2018-2023 Kevin O'Connor <kevin@koconnor.net>
#           Copyright (C) 2021 Paul McGowan <mental405@gmail.com>
#           Copyright (C) 2023 Alan Smith <alan@airpost.net>
# Modified: Scott / BlackStump - return_to_last_probe_position option,
#           attach/detach speed config
#
# Updated for current Klipper master API:
#
#   HomingViaProbeHelper(config, position_endstop_z, query_endstop_cb=None)
#       - lightweight: just registers probe:z_virtual_endstop pin
#       - does NOT own the probe session
#
#   DescendToEndstopHelper(config, mcu_probe, probe_offsets, param_helper)
#       - owns descend_until_trigger(), pull_trigger_positions(), clear_trigger_positions()
#
#   SampleAveragingHelper(config, param_helper, start_session_cb)
#       - owns multi-sample averaging; start_probe_session() returns self
#       - internally calls start_session_cb(gcmd) -> hw_session with
#         run_probe() / pull_probed_results() / end_probe_session()
#
#   ProbeParameterHelper(config)  - no extra args
#   ProbeOffsetsHelper(config)    - get_offsets(gcmd=None)
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from . import probe
from mcu import MCU_endstop
from math import sin, cos, atan2, pi, sqrt
import logging

PROBE_VERIFY_DELAY = .1

PROBE_UNKNOWN  = 0
PROBE_ATTACHED = 1
PROBE_DOCKED   = 2

MULTI_OFF   = 0
MULTI_FIRST = 1
MULTI_ON    = 2

HINT_VERIFICATION_ERROR = """
{0}: A probe attachment verification method
was not provided. A method to verify the probe attachment
state must be specified to prevent unintended behavior.

At least one of the following must be specified:
'check_open_attach', 'probe_sense_pin', 'dock_sense_pin'
"""

HINT_VIRTUAL_ENDSTOP_ERROR = """
{0}: Using a 'probe:z_virtual_endstop' Z endstop is
incompatible with 'approach_position'/'dock_position'
containing a Z coordinate.

If the toolhead doesn't need to move in Z to reach the
dock then no Z coordinate should be specified in
'approach_position'/'dock_position'.
"""

######################################################################
# Pin polling helper
######################################################################

class PinPollingHelper:
    def __init__(self, config, endstop):
        self.printer = config.get_printer()
        self.query_endstop    = endstop
        self.last_verify_time  = 0
        self.last_verify_state = None

    def query_pin(self, curtime):
        if (curtime > (self.last_verify_time + PROBE_VERIFY_DELAY)
                or self.last_verify_state is None):
            self.last_verify_time = curtime
            toolhead   = self.printer.lookup_object('toolhead')
            query_time = toolhead.get_last_move_time()
            self.last_verify_state = not not self.query_endstop(query_time)
        return self.last_verify_state

    def query_pin_inv(self, curtime):
        return not self.query_pin(curtime)

######################################################################
# Probe attachment state detector
######################################################################

class ProbeState:
    def __init__(self, config, aProbe):
        self.printer = config.get_printer()

        if (not config.fileconfig.has_option(config.section,
                                             'check_open_attach')
                and not config.fileconfig.has_option(config.section,
                                                     'probe_sense_pin')
                and not config.fileconfig.has_option(config.section,
                                                     'dock_sense_pin')):
            raise self.printer.config_error(
                HINT_VERIFICATION_ERROR.format(aProbe.name))

        self.printer.register_event_handler('klippy:ready',
                                            self._handle_ready)

        ppins = self.printer.lookup_object('pins')

        def configEndstop(pin):
            mcu_endstop = ppins.setup_pin('endstop', pin)
            return PinPollingHelper(config, mcu_endstop.query_endstop)

        # Default: use probe endstop as dummy sensor (inverted = open when attached)
        ehelper = PinPollingHelper(config, aProbe.query_endstop)
        self.probe_sense_pin = ehelper.query_pin_inv

        # probe_sense_pin (optional)
        probe_sense_pin = config.get('probe_sense_pin', None)
        if probe_sense_pin is not None:
            self.probe_sense_pin = configEndstop(probe_sense_pin).query_pin

        # check_open_attach overrides probe_sense_pin when present
        if config.fileconfig.has_option(config.section, 'check_open_attach'):
            check_open_attach = config.getboolean('check_open_attach')
            if check_open_attach:
                self.probe_sense_pin = ehelper.query_pin_inv
            else:
                self.probe_sense_pin = ehelper.query_pin

        # dock_sense_pin (optional)
        self.dock_sense_pin = None
        dock_sense_pin = config.get('dock_sense_pin', None)
        if dock_sense_pin is not None:
            self.dock_sense_pin = configEndstop(dock_sense_pin).query_pin

        self.last_verify_time  = 0
        self.last_verify_state = PROBE_UNKNOWN

    def _handle_ready(self):
        self.last_verify_time  = 0
        self.last_verify_state = PROBE_UNKNOWN

    def get_probe_state(self):
        curtime = self.printer.get_reactor().monotonic()
        return self.get_probe_state_with_time(curtime)

    def get_probe_state_with_time(self, curtime):
        if (self.last_verify_state == PROBE_UNKNOWN
                or curtime > self.last_verify_time + PROBE_VERIFY_DELAY):
            self.last_verify_time  = curtime
            self.last_verify_state = PROBE_UNKNOWN
            a = self.probe_sense_pin(curtime)
            if self.dock_sense_pin is not None:
                d = self.dock_sense_pin(curtime)
                if a and not d:
                    self.last_verify_state = PROBE_ATTACHED
                elif d and not a:
                    self.last_verify_state = PROBE_DOCKED
            else:
                self.last_verify_state = PROBE_ATTACHED if a else PROBE_DOCKED
        return self.last_verify_state

######################################################################
# Probe session wrapper
# Bridges the SampleAveragingHelper "start_session_cb" interface to
# DockableProbe.  SampleAveragingHelper calls start_session_cb(gcmd)
# and expects the returned object to have:
#   run_probe(gcmd)
#   pull_probed_results() -> list
#   end_probe_session()
######################################################################

class DockableProbeSession:
    """Thin session object returned to SampleAveragingHelper."""
    def __init__(self, dp, descend_helper):
        self.dp             = dp
        self.descend_helper = descend_helper

    def run_probe(self, gcmd):
        self.descend_helper.descend_until_trigger(gcmd)

    def pull_probed_results(self):
        return self.descend_helper.pull_trigger_positions()

    def end_probe_session(self):
        self.descend_helper.clear_trigger_positions()
        self.dp.multi_probe_end()

######################################################################
# Main DockableProbe class
######################################################################

class DockableProbe:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode   = self.printer.lookup_object('gcode')
        self.name    = config.get_name()

        # ----------------------------------------------------------------
        # Configuration options
        # ----------------------------------------------------------------
        self.position_endstop = config.getfloat('z_offset')
        self.x_offset         = config.getfloat('x_offset', 0.)
        self.y_offset         = config.getfloat('y_offset', 0.)
        self.speed            = config.getfloat('speed', 5.0, above=0.)
        self.lift_speed       = config.getfloat('lift_speed',
                                                self.speed, above=0.)
        self.dock_retries       = config.getint('dock_retries', 0)
        self.auto_attach_detach = config.getboolean('auto_attach_detach', True)
        self.travel_speed     = config.getfloat('travel_speed',
                                                self.speed, above=0.)
        self.attach_speed     = config.getfloat('attach_speed',
                                                self.speed, above=0.)
        self.detach_speed     = config.getfloat('detach_speed',
                                                self.speed, above=0.)
        self.sample_retract_dist = config.getfloat('sample_retract_dist',
                                                   2., above=0.)
        self.return_to_last_probe_position_after_detach = \
            config.getboolean(
                'return_to_last_probe_position_after_detach', True)

        # ----------------------------------------------------------------
        # Dock positions
        # ----------------------------------------------------------------
        self.approach_position = self._parse_coord(config, 'approach_position')
        self.detach_position   = self._parse_coord(config, 'detach_position')
        self.dock_position     = self._parse_coord(config, 'dock_position')

        self.z_hop = config.getfloat('z_hop', 0., above=0.)

        self.dock_requires_z = (len(self.approach_position) > 2
                                or len(self.dock_position) > 2)

        self.dock_angle, self.approach_distance = self._get_vector(
            self.dock_position, self.approach_position)
        self.detach_angle, self.detach_distance = self._get_vector(
            self.dock_position, self.detach_position)

        # ----------------------------------------------------------------
        # Endstop pin
        # ----------------------------------------------------------------
        ppins = self.printer.lookup_object('pins')
        self.mcu_endstop = ppins.setup_pin('endstop', config.get('pin'))

        # klipper_z_calibration compatibility
        self.mcu_probe = self.mcu_endstop

        # Endstop wrappers (needed by DescendToEndstopHelper via LookupZSteppers)
        self.get_mcu       = self.mcu_endstop.get_mcu
        self.add_stepper   = self.mcu_endstop.add_stepper
        self.get_steppers  = self.mcu_endstop.get_steppers
        self.home_wait     = self.mcu_endstop.home_wait
        self.query_endstop = self.mcu_endstop.query_endstop

        self.finish_home_complete = self.wait_trigger_complete = None

        # ----------------------------------------------------------------
        # Probe helper classes  (current Klipper master)
        # ----------------------------------------------------------------
        self.probe_offsets = probe.ProbeOffsetsHelper(config)
        self.param_helper  = probe.ProbeParameterHelper(config)

        # DescendToEndstopHelper owns the actual Z-descend / endstop trigger.
        # It calls self.add_stepper (via LookupZSteppers) so we must exist first.
        self.descend_helper = probe.DescendToEndstopHelper(
            config, self, self.probe_offsets, self.param_helper)

        # HomingViaProbeHelper just registers the probe:z_virtual_endstop pin.
        # Signature: (config, position_endstop_z_float, query_endstop_cb=None)
        probe.HomingViaProbeHelper(
            config,
            self.position_endstop,
            self.mcu_endstop.query_endstop)

        # SampleAveragingHelper handles multi-sample averaging.
        # It calls start_session_cb(gcmd) -> session object.
        self.probe_session = probe.SampleAveragingHelper(
            config, self.param_helper, self._start_hw_session)

        # ProbeCommandHelper registers PROBE, QUERY_PROBE, PROBE_CALIBRATE etc.
        self.cmd_helper = probe.ProbeCommandHelper(
            config, self, self.mcu_endstop.query_endstop)

        # ----------------------------------------------------------------
        # State
        # ----------------------------------------------------------------
        self.last_z      = -9999
        self.multi       = MULTI_OFF
        self._last_homed = None

        pstate = ProbeState(config, self)
        self.get_probe_state  = pstate.get_probe_state
        self.last_probe_state = PROBE_UNKNOWN
        self.probe_states = {
            PROBE_ATTACHED: 'ATTACHED',
            PROBE_DOCKED:   'DOCKED',
            PROBE_UNKNOWN:  'UNKNOWN',
        }

        # ----------------------------------------------------------------
        # GCode commands
        # ----------------------------------------------------------------
        self.gcode.register_command('QUERY_DOCKABLE_PROBE',
            self.cmd_QUERY_DOCKABLE_PROBE,
            desc=self.cmd_QUERY_DOCKABLE_PROBE_help)
        self.gcode.register_command('MOVE_TO_APPROACH_PROBE',
            self.cmd_MOVE_TO_APPROACH_PROBE,
            desc=self.cmd_MOVE_TO_APPROACH_PROBE_help)
        self.gcode.register_command('MOVE_TO_DOCK_PROBE',
            self.cmd_MOVE_TO_DOCK_PROBE,
            desc=self.cmd_MOVE_TO_DOCK_PROBE_help)
        self.gcode.register_command('MOVE_TO_EXTRACT_PROBE',
            self.cmd_MOVE_TO_EXTRACT_PROBE,
            desc=self.cmd_MOVE_TO_EXTRACT_PROBE_help)
        self.gcode.register_command('MOVE_TO_INSERT_PROBE',
            self.cmd_MOVE_TO_INSERT_PROBE,
            desc=self.cmd_MOVE_TO_INSERT_PROBE_help)
        self.gcode.register_command('MOVE_TO_DETACH_PROBE',
            self.cmd_MOVE_TO_DETACH_PROBE,
            desc=self.cmd_MOVE_TO_DETACH_PROBE_help)
        self.gcode.register_command('SET_DOCKABLE_PROBE',
            self.cmd_SET_DOCKABLE_PROBE,
            desc=self.cmd_SET_DOCKABLE_PROBE_help)
        self.gcode.register_command('ATTACH_PROBE',
            self.cmd_ATTACH_PROBE,
            desc=self.cmd_ATTACH_PROBE_help)
        self.gcode.register_command('DETACH_PROBE',
            self.cmd_DETACH_PROBE,
            desc=self.cmd_DETACH_PROBE_help)

        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)

    # ------------------------------------------------------------------
    # Session factory called by SampleAveragingHelper
    # ------------------------------------------------------------------

    def _start_hw_session(self, gcmd):
        """Called by SampleAveragingHelper.start_probe_session().
        Attaches the probe, resets the descend helper, and returns a
        session object that SampleAveragingHelper will drive."""
        self.multi_probe_begin()
        self.descend_helper.clear_trigger_positions()
        return DockableProbeSession(self, self.descend_helper)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _parse_coord(self, config, name, expected_dims=3):
        val = config.get(name)
        error_msg = "Unable to parse {0} in {1}: {2}"
        if not val:
            return None
        try:
            vals = [float(x.strip()) for x in val.split(',')]
        except Exception as e:
            raise config.error(error_msg.format(name, self.name, str(e)))
        supplied_dims = len(vals)
        if not 2 <= supplied_dims <= expected_dims:
            raise config.error(error_msg.format(name, self.name,
                                                "Invalid number of coordinates"))
        p = [None] * supplied_dims
        p[:supplied_dims] = vals
        return p

    # ------------------------------------------------------------------
    # Public probe API
    # ------------------------------------------------------------------

    def get_probe_params(self, gcmd=None):
        return self.param_helper.get_probe_params(gcmd)

    def get_offsets(self, gcmd=None):
        return self.probe_offsets.get_offsets(gcmd)

    def get_status(self, eventtime):
        status = self.cmd_helper.get_status(eventtime)
        status['last_status'] = self.last_probe_state
        return status

    def start_probe_session(self, gcmd):
        return self.probe_session.start_probe_session(gcmd)

    def get_position_endstop(self):
        return self.position_endstop

    # ------------------------------------------------------------------
    # klippy:connect handler
    # ------------------------------------------------------------------

    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        if not self.dock_requires_z:
            return
        query_endstops = self.printer.lookup_object('query_endstops')
        for endstop, name in query_endstops.endstops:
            if name == 'z':
                if not isinstance(endstop, MCU_endstop):
                    raise self.printer.config_error(
                        HINT_VIRTUAL_ENDSTOP_ERROR.format(self.name))

    ##################################################################
    # GCode Commands
    ##################################################################

    cmd_QUERY_DOCKABLE_PROBE_help = ("Prints the current probe state, "
                                     "valid states: UNKNOWN, ATTACHED, DOCKED")
    def cmd_QUERY_DOCKABLE_PROBE(self, gcmd):
        self.last_probe_state = self.get_probe_state()
        state = self.probe_states[self.last_probe_state]
        gcmd.respond_info('Probe Status: %s' % (state,))

    cmd_MOVE_TO_APPROACH_PROBE_help = "Move close to the probe dock before attaching"
    def cmd_MOVE_TO_APPROACH_PROBE(self, gcmd):
        self._align_z()
        if self._check_distance(dist=self.approach_distance):
            self._align_to_vector(self.dock_angle)
        else:
            self._move_to_vector(self.dock_angle)
        if len(self.approach_position) > 2:
            self.toolhead.manual_move(
                [None, None, self.approach_position[2]], self.attach_speed)
        self.toolhead.manual_move(
            [self.approach_position[0], self.approach_position[1], None],
            self.attach_speed)

    cmd_MOVE_TO_DOCK_PROBE_help = "Move to connect the toolhead/dock to the probe"
    def cmd_MOVE_TO_DOCK_PROBE(self, gcmd):
        if len(self.dock_position) > 2:
            self.toolhead.manual_move(
                [None, None, self.dock_position[2]], self.attach_speed)
        self.toolhead.manual_move(
            [self.dock_position[0], self.dock_position[1], None],
            self.attach_speed)

    cmd_MOVE_TO_EXTRACT_PROBE_help = "Move away from the dock with the probe attached"
    def cmd_MOVE_TO_EXTRACT_PROBE(self, gcmd):
        self.cmd_MOVE_TO_APPROACH_PROBE(gcmd)

    cmd_MOVE_TO_INSERT_PROBE_help = "Move near the dock with probe attached before detaching"
    def cmd_MOVE_TO_INSERT_PROBE(self, gcmd):
        self.cmd_MOVE_TO_APPROACH_PROBE(gcmd)

    cmd_MOVE_TO_DETACH_PROBE_help = "Move away from the dock to detach the probe"
    def cmd_MOVE_TO_DETACH_PROBE(self, gcmd):
        if len(self.detach_position) > 2:
            self.toolhead.manual_move(
                [None, None, self.detach_position[2]], self.detach_speed)
        self.toolhead.manual_move(
            [self.detach_position[0], self.detach_position[1], None],
            self.detach_speed)

    cmd_SET_DOCKABLE_PROBE_help = "Set probe parameters"
    def cmd_SET_DOCKABLE_PROBE(self, gcmd):
        auto = gcmd.get('AUTO_ATTACH_DETACH', None)
        if auto is None:
            return
        self.auto_attach_detach = (int(auto) == 1)

    cmd_ATTACH_PROBE_help = "Check probe status and attach probe"
    def cmd_ATTACH_PROBE(self, gcmd):
        return_pos = self.toolhead.get_position()
        self.attach_probe(return_pos)

    cmd_DETACH_PROBE_help = "Check probe status and detach probe"
    def cmd_DETACH_PROBE(self, gcmd):
        return_pos = self.toolhead.get_position()
        self.detach_probe(return_pos)

    # ------------------------------------------------------------------
    # Attach / detach logic
    # ------------------------------------------------------------------

    def attach_probe(self, return_pos=None):
        retry = 0
        while (self.get_probe_state() != PROBE_ATTACHED
               and retry < self.dock_retries + 1):
            if self.get_probe_state() != PROBE_DOCKED:
                raise self.printer.command_error(
                    'Attach Probe: Probe not detected in dock, aborting')
            self.gcode.run_script_from_command(
                "MOVE_TO_APPROACH_PROBE\n"
                "MOVE_TO_DOCK_PROBE\n"
                "MOVE_TO_EXTRACT_PROBE\n")
            retry += 1

        if self.get_probe_state() != PROBE_ATTACHED:
            raise self.printer.command_error('Probe attach failed!')

        if self.return_to_last_probe_position_after_detach and return_pos:
            if not self._check_distance(return_pos, self.approach_distance):
                self.toolhead.manual_move(
                    [return_pos[0], return_pos[1], None], self.travel_speed)
                self.toolhead.manual_move(
                    [None, None, return_pos[2]], self.travel_speed)

    def detach_probe(self, return_pos=None):
        retry = 0
        while (self.get_probe_state() != PROBE_DOCKED
               and retry < self.dock_retries + 1):
            self.gcode.run_script_from_command(
                "MOVE_TO_INSERT_PROBE\n"
                "MOVE_TO_DOCK_PROBE\n"
                "MOVE_TO_DETACH_PROBE\n")
            retry += 1

        if self.get_probe_state() != PROBE_DOCKED:
            raise self.printer.command_error('Probe detach failed!')

        if self.return_to_last_probe_position_after_detach and return_pos:
            if not self._check_distance(return_pos, self.detach_distance):
                self.toolhead.manual_move(
                    [return_pos[0], return_pos[1], None], self.travel_speed)
                self.toolhead.manual_move(
                    [None, None, return_pos[2]], self.travel_speed)

    def auto_detach_probe(self, return_pos=None):
        if self.get_probe_state() == PROBE_DOCKED:
            return
        if self.auto_attach_detach:
            self.detach_probe(return_pos)

    def auto_attach_probe(self, return_pos=None):
        if self.get_probe_state() == PROBE_ATTACHED:
            return
        if not self.auto_attach_detach:
            raise self.printer.command_error(
                "Cannot probe, probe is not attached "
                "and auto-attach is disabled")
        self.attach_probe(return_pos)

    ##################################################################
    # Geometry / toolhead movement helpers
    ##################################################################

    def _move_to_vector(self, angle):
        x, y = self._get_point_on_vector(self.dock_position[:2],
                                         angle,
                                         self.approach_distance)
        self.toolhead.manual_move([x, y, None], self.travel_speed)

    def _align_to_vector(self, angle):
        approach = self._get_intercept(self.toolhead.get_position(),
                                       angle + (pi / 2),
                                       self.dock_position,
                                       angle)
        self.toolhead.manual_move([approach[0], approach[1], None],
                                  self.attach_speed)

    def _check_distance(self, pos=None, dist=None):
        if pos is None:
            pos = self.toolhead.get_position()
        dock = self.dock_position
        return dist > sqrt((pos[0] - dock[0]) ** 2 +
                           (pos[1] - dock[1]) ** 2)

    def _get_point_on_vector(self, point, angle, magnitude=1):
        x = point[0] - magnitude * cos(angle)
        y = point[1] - magnitude * sin(angle)
        return (x, y)

    def _get_intercept(self, point1, angle1, point2, angle2):
        x1, y1 = point1[:2]
        x2, y2 = self._get_point_on_vector(point1, angle1, 10.0)
        x3, y3 = point2[:2]
        x4, y4 = self._get_point_on_vector(point2, angle2, 10.0)
        det1 = (x1 * y2) - (y1 * x2)
        det2 = (x3 * y4) - (y3 * x4)
        d    = ((x1 - x2) * (y3 - y4)) - ((y1 - y2) * (x3 - x4))
        x    = float((det1 * (x3 - x4)) - ((x1 - x2) * det2)) / d
        y    = float((det1 * (y3 - y4)) - ((y1 - y2) * det2)) / d
        return (x, y)

    def _get_vector(self, point1, point2):
        x1, y1 = point1[:2]
        x2, y2 = point2[:2]
        magnitude = sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
        angle     = atan2(y2 - y1, x2 - x1) + pi
        return angle, magnitude

    def _align_z(self):
        curtime     = self.printer.get_reactor().monotonic()
        homed_axes  = self.toolhead.get_status(curtime)['homed_axes']
        self._last_homed = homed_axes
        if self.dock_requires_z:
            self._align_z_required()
        if self.z_hop > 0.0:
            if 'z' in self._last_homed:
                tpos = self.toolhead.get_position()
                if tpos[2] < self.z_hop:
                    self.toolhead.manual_move([None, None, self.z_hop],
                                              self.lift_speed)
            else:
                self._force_z_hop()

    def _align_z_required(self):
        if 'z' not in self._last_homed:
            raise self.printer.command_error(
                "Cannot attach/detach probe, must home Z axis first")
        self.toolhead.manual_move(
            [None, None, self.approach_position[2]], self.lift_speed)

    def _force_z_hop(self):
        this_z = self.toolhead.get_position()[2]
        if self.last_z == this_z:
            return
        tpos = self.toolhead.get_position()
        self.toolhead.set_position(
            [tpos[0], tpos[1], 0.0, tpos[3]], homing_axes=[2])
        self.toolhead.manual_move([None, None, self.z_hop], self.lift_speed)
        kin = self.toolhead.get_kinematics()
        kin.note_z_not_homed()
        self.last_z = self.toolhead.get_position()[2]

    ##################################################################
    # Multi-probe begin/end  (called from _start_hw_session / session end)
    ##################################################################

    def multi_probe_begin(self):
        self.multi = MULTI_FIRST
        return_pos = self.toolhead.get_position()
        self.auto_attach_probe(return_pos)

    def multi_probe_end(self):
        self.multi = MULTI_OFF
        return_pos = self.toolhead.get_position()
        # Lift slightly so the probe isn't triggered when checking state
        self.toolhead.manual_move(
            [None, None, return_pos[2] + 2], self.travel_speed)
        self.auto_detach_probe(return_pos)

    ##################################################################
    # MCU endstop interface forwarded to mcu_endstop
    # (needed so DescendToEndstopHelper can call probing_move on us)
    ##################################################################

    def home_start(self, print_time, sample_time, sample_count,
                   rest_time, triggered=True):
        self.finish_home_complete = self.mcu_endstop.home_start(
            print_time, sample_time, sample_count, rest_time, triggered)
        r = self.printer.get_reactor()
        self.wait_trigger_complete = r.register_callback(
            self.wait_for_trigger)
        return self.finish_home_complete

    def wait_for_trigger(self, eventtime):
        self.finish_home_complete.wait()


def load_config(config):
    dockable_probe = DockableProbe(config)
    config.get_printer().add_object('probe', dockable_probe)
    return dockable_probe
