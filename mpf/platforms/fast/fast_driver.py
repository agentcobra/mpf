"""A driver/coil in the fast platform."""
import logging
from typing import Dict, Tuple, Optional

from mpf.core.platform import DriverConfig
from mpf.core.utility_functions import Util
from mpf.platforms.interfaces.driver_platform_interface import DriverPlatformInterface, PulseSettings, HoldSettings

MYPY = False
if MYPY:   # pragma: no cover
    from mpf.platforms.fast.fast import FastHardwarePlatform    # pylint: disable-msg=cyclic-import,unused-import


class FASTDriver(DriverPlatformInterface):

    """Base class for drivers connected to a FAST Controller."""

    # __slots__ = ["log", "autofire", "_autofire_cleared", "config_state", "machine", "platform", "driver_settings",
    #              "send", "platform_settings"]

    def __init__(self, config: DriverConfig, platform: "FastHardwarePlatform", number: str,
                 platform_settings: dict) -> None:
        """Initialise driver."""
        super().__init__(config, number)
        self.log = logging.getLogger('FASTDriver')
        self.autofire = None                        # type: Optional[Tuple[str, Dict[str, float]]]
        self._autofire_cleared = False
        self.config_state = None                    # type: Optional[Tuple[float, float, float]]
        self.machine = platform.machine
        self.platform = platform
        self.driver_settings = {}                   # type: Dict[str, str]
        self.connection = platform.serial_connections['net']
        self.platform_settings = platform_settings

        self.log.debug("Driver Settings: %s", self.driver_settings)

    def get_board_name(self):
        """Return the board of this driver."""
        if self.platform.is_retro:
            return f"FAST Retro ({self.platform.machine_type.upper()})"

        coil_index = 0
        number = Util.hex_string_to_int(self.number)
        for board_obj in self.platform.io_boards.values():
            if coil_index <= number < coil_index + board_obj.driver_count:
                return f"FAST Board {str(board_obj.node_id)}"
            coil_index += board_obj.driver_count

        # fall back if not found
        return "FAST Unknown Board"

    def get_hold_pwm_for_cmd(self, power):
        """Return a hex string for a float power setting for hold."""
        if self.platform_settings.get('hold_pwm_patter'):
            return self.platform_settings['hold_pwm_patter']

        return self.get_pwm_for_cmd(power)

    @classmethod
    def get_pwm_for_cmd(cls, power: float):
        """Return a hex string for a float power setting."""
        # use PWM8 if sufficiently accurate
        if (power * 8) - int(power * 8) < 0.025:
            return Util.pwm8_to_hex_string(int(power * 8)).upper()

        return Util.pwm32_to_hex_string(int(power * 32)).upper()

    def get_recycle_ms_for_cmd(self, recycle, pulse_ms):
        """Return recycle ms."""
        if not recycle:
            return "00"
        if self.platform_settings.get('recycle_ms') is not None:
            return Util.int_to_hex_string(self.platform_settings['recycle_ms'])

        # default recycle_ms to pulse_ms * 2
        if pulse_ms * 2 > 255:
            return "FF"

        return Util.int_to_hex_string(pulse_ms * 2)

    @classmethod
    def get_control_for_cmd(cls, switch1, switch2=None):
        """Return control bytes."""
        control = 0x01  # Driver enabled
        if switch1.invert:
            control += 0x10
        if switch2 and switch2.invert:
            control += 0x20

        return Util.int_to_hex_string(int(control))

    async def reset(self):
        """Reset a driver."""
        self.log.debug("Resetting driver %s", self.driver_settings)

        cmd = f'{self.connection.driver_cmd}:{self.number},00,00,00'

        self.connection.send_with_confirmation(cmd, self.connection.driver_cmd)

    def disable(self):
        """Disable (turn off) this driver."""
        cmd = f'{self.connection.trigger_cmd}:{self.number},02'

        self.log.debug("Sending Disable Command: %s", cmd)
        self.connection.send_and_forget(cmd)  # TODO remove config lookups

        self._reenable_autofire_if_configured()

        # reenable the autofire
        if self.autofire:
            cmd = f'{self.connection.trigger_cmd}:{self.number},00'
            self.log.debug("Re-enabling auto fire mode: %s", cmd)
            self.connection.send_and_forget(cmd)  # TODO remove config lookups

    def set_autofire(self, autofire_cmd, pulse_duration, pulse_power, hold_power):
        """Set an autofire."""
        self.autofire = autofire_cmd, (pulse_duration, pulse_power, hold_power)
        self.config_state = pulse_duration, pulse_power, hold_power
        self._autofire_cleared = False
        self.log.debug("Writing hardware rule: %s", autofire_cmd)
        self.connection.send_with_confirmation(autofire_cmd, self.connection.driver_cmd)

    def clear_autofire(self, config_cmd, number):
        """Clear autofire."""
        cmd = '{}{},81'.format(config_cmd, number)
        self.log.debug("Clearing hardware rule: %s", cmd)
        self.connection.send_with_confirmation(cmd, self.connection.driver_cmd)
        self.autofire = None
        self.config_state = None

    def enable(self, pulse_settings: PulseSettings, hold_settings: HoldSettings):
        """Enable (turn on) this driver."""
        config_state = pulse_settings.duration, pulse_settings.power, hold_settings.power
        if self.autofire and self.config_state == config_state:
            # If this driver is also configured for an autofire rule, we just
            # manually trigger it with the trigger_cmd and manual on ('03')
            cmd = f'{self.connection.trigger_cmd}:{self.number},03'
        else:
            # Otherwise we send a full config command, trigger C1 (logic triggered
            # and drive now) switch ID 00, mode 18 (latched)
            self._autofire_cleared = True

            cmd = '{}:{},C1,00,18,{},{},{},{}'.format(
                self.connection.driver_cmd,
                self.number,
                Util.int_to_hex_string(pulse_settings.duration),
                self.get_pwm_for_cmd(pulse_settings.power),
                self.get_hold_pwm_for_cmd(hold_settings.power),
                self.get_recycle_ms_for_cmd(self.config.default_recycle, pulse_settings.duration)
            )
            self.config_state = (pulse_settings.duration, pulse_settings.duration, hold_settings.power)

        self.log.debug("Sending Enable Command: %s", cmd)
        self.connection.send_and_forget(cmd)  # TODO send_txt_with_ack

    def timed_enable(self, pulse_settings: PulseSettings, hold_settings: HoldSettings):
        """Pulse and hold this driver for a specified duration."""
        self._pulse(pulse_settings, hold_settings)

    def pulse(self, pulse_settings: PulseSettings):
        """Pulse this driver."""
        self._pulse(pulse_settings)

    def _pulse(self, pulse_settings: PulseSettings, hold_settings: HoldSettings = None):
        """Pulse this driver, with an optional hold setting.

        The FAST platform supports pulse and hold configuration in the same command, so
        this method can be used for both pulse() and timed_enable() behavior.
        """
        hex_ms_string = Util.int_to_hex_string(pulse_settings.duration)
        if hold_settings is not None:
            hold_power = self.get_hold_pwm_for_cmd(hold_settings.power)
            hold_ms = Util.int_to_hex_string(hold_settings.duration, True)
            config_state = (pulse_settings.duration, pulse_settings.power, hold_settings.power)
        else:
            hold_power = '00'
            hold_ms = '00'
            config_state = (pulse_settings.duration, pulse_settings.power, 0)

        # reconfigure if we have to
        if not self.config_state or self.config_state[0] != config_state[0] or self.config_state[1] != config_state[1]:

            self.config_state = config_state
            self._autofire_cleared = True

            # The 89 trigger will write this rule to the driver and pulse it immediately after
            cmd = '{}:{},89,00,10,{},{},{},{},00'.format(
                self.connection.driver_cmd,
                self.number,
                hex_ms_string,
                self.get_pwm_for_cmd(pulse_settings.power),
                hold_ms,
                hold_power
            )
            self.connection.send_with_confirmation(cmd, self.connection.driver_cmd)
        else:
            # Trigger the driver directly using the existing configuration
            cmd = '{}:{},01'.format(self.connection.trigger_cmd, self.number)
            self.connection.send_and_forget(cmd)

        # restore autofire
        self._reenable_autofire_if_configured()

        return Util.hex_string_to_int(hex_ms_string)

    def _reenable_autofire_if_configured(self):
        """Reenable autofire if configured."""
        if self.autofire and self._autofire_cleared:
            self._autofire_cleared = False
            cmd = self.autofire[0]
            self.config_state = self.autofire[1]

            self.log.debug("Re-enabling auto fire mode: %s", cmd)
            self.connection.send_with_confirmation(cmd, self.connection.driver_cmd)
