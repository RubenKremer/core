"""Support for LibreHardwareMonitor Sensor Platform."""

from __future__ import annotations

import logging
import math

from librehardwaremonitor_api.model import LibreHardwareMonitorSensorData

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import LibreHardwareMonitorCoordinator
from .const import BYTES_PER_KB, DOMAIN
from .coordinator import LibreHardwareMonitorConfigEntry

# Coordinator is used to centralize the data updates
PARALLEL_UPDATES = 0

STATE_MIN_VALUE = "min_value"
STATE_MAX_VALUE = "max_value"

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LibreHardwareMonitorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the LibreHardwareMonitor platform."""
    lhm_coordinator = config_entry.runtime_data

    async_add_entities(
        LibreHardwareMonitorSensor(lhm_coordinator, config_entry, sensor_data)
        for sensor_data in lhm_coordinator.data.sensor_data.values()
    )


class LibreHardwareMonitorSensor(
    CoordinatorEntity[LibreHardwareMonitorCoordinator], SensorEntity
):
    """Sensor to display information from LibreHardwareMonitor."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LibreHardwareMonitorCoordinator,
        config_entry: LibreHardwareMonitorConfigEntry,
        sensor_data: LibreHardwareMonitorSensorData,
    ) -> None:
        """Initialize an LibreHardwareMonitor sensor."""
        super().__init__(coordinator)

        self._attr_name: str = sensor_data.name
        self.value: str | None = None

        self._attr_extra_state_attributes: dict[str, str] = {
            STATE_MIN_VALUE: self._format_number_value(sensor_data.min),
            STATE_MAX_VALUE: self._format_number_value(sensor_data.max),
        }
        self._attr_native_unit_of_measurement = sensor_data.unit
        self._attr_unique_id: str = (
            f"lhm_{config_entry.entry_id}_{sensor_data.sensor_id}"
        )

        self._sensor_id: str = sensor_data.sensor_id

        # Hardware device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{config_entry.entry_id}_{sensor_data.device_id}")},
            name=sensor_data.device_name,
            model=sensor_data.device_type,
        )

        # Initialize with normalized data
        self._update_sensor_data(sensor_data)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if sensor_data := self.coordinator.data.sensor_data.get(self._sensor_id):
            self._update_sensor_data(sensor_data)
        else:
            self.value = None

        super()._handle_coordinator_update()

    def _update_sensor_data(self, sensor_data: LibreHardwareMonitorSensorData) -> None:
        """Update sensor data with normalization applied."""
        value = sensor_data.value or ""  # Convert None to empty string

        # cast to str to satisfy type checker
        normalized_value, normalized_unit = self._normalize_data_rate(
            value, str(sensor_data.unit) if sensor_data.unit is not None else None
        )

        self.value = normalized_value
        self._attr_native_unit_of_measurement = normalized_unit

        self._attr_extra_state_attributes = {
            STATE_MIN_VALUE: self._format_number_value(sensor_data.min),
            STATE_MAX_VALUE: self._format_number_value(sensor_data.max),
        }

    @property
    def native_value(self) -> str | None:
        """Return the formatted sensor value or None if no value is available."""
        if self.value is not None and self.value != "-":
            return self._format_number_value(self.value)
        return None

    @staticmethod
    def _format_number_value(number_str: str) -> str:
        """Format number string by converting European comma to decimal point."""
        return number_str.replace(",", ".")

    @staticmethod
    def _is_valid_numeric_value(value: str) -> bool:
        """Check if value can be converted to a valid number."""
        if not value or value == "-":
            return False
        try:
            numeric_value = float(value.replace(",", "."))
            # Check for NaN, infinity, and negative infinity
            return not (math.isnan(numeric_value) or math.isinf(numeric_value))
        except (ValueError, TypeError):
            return False

    def _normalize_data_rate(self, value: str, unit: str | None) -> tuple[str, str]:
        """Normalize data rate values to MB/s for consistency.

        Converts kB/s, MB/s, and GB/s to MB/s to prevent unit changes
        that confuse Home Assistant's data logging and statistics.
        """
        # Early return if unit is None
        if unit is None:
            return value, ""

        if not value or value == "-":
            return value, unit

        try:
            # LibreHardwareMonitor uses European decimal separator (comma)
            # Convert to standard decimal point for Python float conversion
            numeric_value = float(value.replace(",", "."))

            # Handle zero values explicitly - no conversion needed, just normalize unit
            if numeric_value == 0.0:
                if unit in ("kB/s", "MB/s", "GB/s"):
                    return "0.0", "MB/s"
                return value, unit

            # Only normalize known data rate units
            if unit not in ("kB/s", "MB/s", "GB/s"):
                return value, unit

            # Normalize to MB/s with proper rounding
            if unit == "kB/s":
                normalized_value = round(
                    numeric_value / BYTES_PER_KB, 3
                )  # kB to MB, 3 decimal places
                return str(normalized_value), "MB/s"
            if unit == "MB/s":
                normalized_value = round(
                    numeric_value, 3
                )  # Keep MB/s with 3 decimal places
                return str(normalized_value), "MB/s"
            # Must be GB/s at this point
            normalized_value = round(
                numeric_value * BYTES_PER_KB, 3
            )  # GB to MB, 3 decimal places
            return str(normalized_value), "MB/s"

        except (ValueError, TypeError, OverflowError) as err:
            # Log conversion errors for debugging but don't crash
            _LOGGER.debug("Failed to normalize data rate %s %s: %s", value, unit, err)
            return value, unit
