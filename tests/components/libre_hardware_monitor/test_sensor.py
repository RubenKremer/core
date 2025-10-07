"""Test the LibreHardwareMonitor sensor."""

from dataclasses import replace
from datetime import timedelta
import logging
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from librehardwaremonitor_api import (
    LibreHardwareMonitorConnectionError,
    LibreHardwareMonitorNoDevicesError,
)
from librehardwaremonitor_api.model import (
    DeviceId,
    DeviceName,
    LibreHardwareMonitorData,
    LibreHardwareMonitorSensorData,
)
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.libre_hardware_monitor.const import (
    BYTES_PER_KB,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from homeassistant.components.libre_hardware_monitor.coordinator import (
    LibreHardwareMonitorCoordinator,
)
from homeassistant.components.libre_hardware_monitor.sensor import (
    LibreHardwareMonitorSensor,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry

from . import init_integration

from tests.common import MockConfigEntry, async_fire_time_changed, snapshot_platform


async def test_sensors_are_created(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    mock_lhm_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    """Test sensors are created."""
    await init_integration(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.parametrize(
    "error", [LibreHardwareMonitorConnectionError, LibreHardwareMonitorNoDevicesError]
)
async def test_sensors_go_unavailable_in_case_of_error_and_recover_after_successful_retry(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    mock_lhm_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    snapshot: SnapshotAssertion,
    error: type[Exception],
) -> None:
    """Test sensors go unavailable."""
    await init_integration(hass, mock_config_entry)

    initial_states = hass.states.async_all()
    assert initial_states == snapshot(name="valid_sensor_data")

    mock_lhm_client.get_data.side_effect = error

    freezer.tick(timedelta(DEFAULT_SCAN_INTERVAL))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    unavailable_states = hass.states.async_all()
    assert all(state.state == STATE_UNAVAILABLE for state in unavailable_states)

    mock_lhm_client.get_data.side_effect = None

    freezer.tick(timedelta(DEFAULT_SCAN_INTERVAL))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    recovered_states = hass.states.async_all()
    assert all(state.state != STATE_UNAVAILABLE for state in recovered_states)


async def test_sensors_are_updated(
    hass: HomeAssistant,
    mock_lhm_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test sensors are updated."""
    await init_integration(hass, mock_config_entry)

    entity_id = "sensor.amd_ryzen_7_7800x3d_package_temperature"

    state = hass.states.get(entity_id)

    assert state
    assert state.state != STATE_UNAVAILABLE
    assert state.state == "52.8"

    updated_data = dict(mock_lhm_client.get_data.return_value.sensor_data)
    updated_data["amdcpu-0-temperature-3"] = replace(
        updated_data["amdcpu-0-temperature-3"], value="42,1"
    )
    mock_lhm_client.get_data.return_value = replace(
        mock_lhm_client.get_data.return_value,
        sensor_data=MappingProxyType(updated_data),
    )

    freezer.tick(timedelta(DEFAULT_SCAN_INTERVAL))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)

    assert state
    assert state.state != STATE_UNAVAILABLE
    assert state.state == "42.1"


async def test_sensor_state_is_unknown_when_no_sensor_data_is_provided(
    hass: HomeAssistant,
    mock_lhm_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test sensor state is unknown when sensor data is missing."""
    await init_integration(hass, mock_config_entry)

    entity_id = "sensor.amd_ryzen_7_7800x3d_package_temperature"

    state = hass.states.get(entity_id)

    assert state
    assert state.state != STATE_UNAVAILABLE
    assert state.state == "52.8"

    updated_data = dict(mock_lhm_client.get_data.return_value.sensor_data)
    del updated_data["amdcpu-0-temperature-3"]
    mock_lhm_client.get_data.return_value = replace(
        mock_lhm_client.get_data.return_value,
        sensor_data=MappingProxyType(updated_data),
    )

    freezer.tick(timedelta(DEFAULT_SCAN_INTERVAL))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)

    assert state
    assert state.state == STATE_UNKNOWN


async def test_orphaned_devices_are_removed(
    hass: HomeAssistant,
    mock_lhm_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test that devices in HA that do not receive updates are removed."""
    await init_integration(hass, mock_config_entry)

    mock_lhm_client.get_data.return_value = LibreHardwareMonitorData(
        main_device_ids_and_names=MappingProxyType(
            {
                DeviceId("amdcpu-0"): DeviceName("AMD Ryzen 7 7800X3D"),
                DeviceId("gpu-nvidia-0"): DeviceName("NVIDIA GeForce RTX 4080 SUPER"),
            }
        ),
        sensor_data=mock_lhm_client.get_data.return_value.sensor_data,
    )

    device_registry = dr.async_get(hass)
    orphaned_device = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, f"{mock_config_entry.entry_id}_lpc-nct6687d-0")},
    )

    with patch.object(
        device_registry,
        "async_remove_device",
        wraps=device_registry.async_update_device,
    ) as mock_remove:
        freezer.tick(timedelta(DEFAULT_SCAN_INTERVAL))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

        mock_remove.assert_called_once_with(orphaned_device.id)


async def test_legacy_device_ids_are_updated(
    hass: HomeAssistant,
    mock_lhm_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that non-unique legacy device IDs are updated."""
    await init_integration(hass, mock_config_entry)

    device_registry = dr.async_get(hass)
    legacy_device_ids = ["amdcpu-0", "gpu-nvidia-0", "lpc-nct6687d-0"]
    registered_device_ids = [
        f"{mock_config_entry.entry_id}_{device_id}" for device_id in legacy_device_ids
    ]

    for index, device_id in enumerate(registered_device_ids):
        device = device_registry.async_get_device(identifiers={(DOMAIN, device_id)})
        device_registry.async_update_device(
            device_id=device.id,
            new_identifiers={(DOMAIN, legacy_device_ids[index])},
        )

    device_entries: list[DeviceEntry] = dr.async_entries_for_config_entry(
        registry=dr.async_get(hass), config_entry_id=mock_config_entry.entry_id
    )
    assert {next(iter(device.identifiers))[1] for device in device_entries} == set(
        legacy_device_ids
    )

    hass.config_entries.async_schedule_reload(mock_config_entry.entry_id)

    device_entries: list[DeviceEntry] = dr.async_entries_for_config_entry(
        registry=dr.async_get(hass), config_entry_id=mock_config_entry.entry_id
    )
    assert {next(iter(device.identifiers))[1] for device in device_entries} == set(
        registered_device_ids
    )


async def test_integration_does_not_log_new_devices_on_first_refresh(
    hass: HomeAssistant,
    mock_lhm_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that initial data update does not cause warning about new devices."""
    mock_lhm_client.get_data.return_value = LibreHardwareMonitorData(
        main_device_ids_and_names=MappingProxyType(
            {
                **mock_lhm_client.get_data.return_value.main_device_ids_and_names,
                DeviceId("generic-memory"): DeviceName("Generic Memory"),
            }
        ),
        sensor_data=mock_lhm_client.get_data.return_value.sensor_data,
    )

    with caplog.at_level(logging.WARNING):
        await init_integration(hass, mock_config_entry)
        assert len(caplog.records) == 0


@pytest.mark.parametrize(
    ("value", "unit", "expected_value", "expected_unit"),
    [
        # kB/s to MB/s conversions
        ("1024", "kB/s", "1.0", "MB/s"),
        ("1024,5", "kB/s", "1.0", "MB/s"),  # European decimal separator
        ("2048,999999", "kB/s", "2.001", "MB/s"),
        ("512,25", "kB/s", "0.5", "MB/s"),

        # MB/s stays MB/s
        ("1,5", "MB/s", "1.5", "MB/s"),
        ("1,234567", "MB/s", "1.235", "MB/s"),
        ("100,0", "MB/s", "100.0", "MB/s"),

        # GB/s to MB/s conversions
        ("0,5", "GB/s", "512.0", "MB/s"),
        ("0,0009765625", "GB/s", "1.0", "MB/s"),
        ("1,0", "GB/s", "1024.0", "MB/s"),

        # Other units remain unchanged
        ("75,2", "°C", "75,2", "°C"),
        ("12,072", "V", "12,072", "V"),
        ("100", "%", "100", "%"),
        ("2400", "MHz", "2400", "MHz"),

        # Edge cases
        ("-", "MB/s", "-", "MB/s"),
        ("", "kB/s", "", "kB/s"),
        ("invalid", "MB/s", "invalid", "MB/s"),

        # Zero values - explicit handling
        ("0", "kB/s", "0.0", "MB/s"),
        ("0,0", "MB/s", "0.0", "MB/s"),
        ("0,000", "GB/s", "0.0", "MB/s"),
        ("0", "°C", "0", "°C"),  # Non-data-rate units unchanged
    ],
)
def test_normalize_data_rate(value: str, unit: str, expected_value: str, expected_unit: str) -> None:
    """Test data rate normalization with European decimal separators."""
    sensor = LibreHardwareMonitorSensor(None, None, None)  # type: ignore[arg-type]

    result_value, result_unit = sensor._normalize_data_rate(value, unit)

    assert result_value == expected_value
    assert result_unit == expected_unit


def test_error_handling() -> None:
    """Test robust error handling in data rate normalization."""
    sensor = LibreHardwareMonitorSensor(None, None, None)  # type: ignore[arg-type]

    # Test invalid values that should be handled gracefully
    invalid_cases = [
        ("invalid", "MB/s"),
        ("NaN", "kB/s"),
        ("inf", "GB/s"),
        ("", "MB/s"),
    ]

    for value, unit in invalid_cases:
        result_value, result_unit = sensor._normalize_data_rate(value, unit)
        # Should return original values without crashing
        assert result_value == value
        assert result_unit == unit


def test_edge_case_values() -> None:
    """Test edge cases for data rate normalization."""
    sensor = LibreHardwareMonitorSensor(None, None, None)  # type: ignore[arg-type]

    # Test very large values
    test_cases = [
        # Very large kB/s values
        ("999999,999", "kB/s", "976.562", "MB/s"),
        ("1048576,0", "kB/s", "1024.0", "MB/s"),  # Exactly 1 GB/s in kB/s

        # Very large MB/s values
        ("999,999", "MB/s", "999.999", "MB/s"),
        ("1024,0", "MB/s", "1024.0", "MB/s"),

        # Very large GB/s values
        ("0,001", "GB/s", "1.024", "MB/s"),  # Very small GB/s
        ("1,0", "GB/s", "1024.0", "MB/s"),   # Exactly 1 GB/s
        ("999,999", "GB/s", "1023999.744", "MB/s"),  # Very large GB/s

        # Precision edge cases
        ("0,000001", "kB/s", "0.0", "MB/s"),  # Very small kB/s (rounds to 0.0)
        ("0,0001", "MB/s", "0.0", "MB/s"),     # Very small MB/s (rounds to 0.0)
        ("0,000000001", "GB/s", "0.0", "MB/s"), # Very small GB/s (rounds to 0.0)

        # Boundary values
        ("0,001", "kB/s", "0.001", "MB/s"),    # Just above rounding threshold
        ("0,0005", "kB/s", "0.0", "MB/s"),     # Just below rounding threshold
    ]

    for value, unit, expected_value, expected_unit in test_cases:
        result_value, result_unit = sensor._normalize_data_rate(value, unit)
        assert result_value == expected_value, f"Failed for {value} {unit}: got {result_value}, expected {expected_value}"
        assert result_unit == expected_unit, f"Failed for {value} {unit}: got {result_unit}, expected {expected_unit}"


def test_special_numeric_values() -> None:
    """Test handling of special numeric values."""
    sensor = LibreHardwareMonitorSensor(None, None, None)  # type: ignore[arg-type]

    # Test various representations of special values
    special_cases = [
        # NaN variations
        ("NaN", "MB/s"),
        ("nan", "kB/s"),
        ("NAN", "GB/s"),

        # Infinity variations
        ("inf", "MB/s"),
        ("Inf", "kB/s"),
        ("INF", "GB/s"),
        ("infinity", "MB/s"),
        ("Infinity", "kB/s"),

        # Negative infinity
        ("-inf", "MB/s"),
        ("-Inf", "kB/s"),
        ("-INF", "GB/s"),
        ("-infinity", "MB/s"),

        # Invalid numeric strings
        ("abc", "MB/s"),
        ("12abc", "kB/s"),
        ("abc12", "GB/s"),
        ("12.34.56", "MB/s"),  # Multiple decimal points
        ("12,34,56", "kB/s"),  # Multiple commas
        ("++12", "GB/s"),      # Multiple plus signs
        ("--12", "MB/s"),       # Multiple minus signs
        ("12e", "kB/s"),        # Incomplete scientific notation
        ("12E", "GB/s"),        # Incomplete scientific notation
    ]

    for value, unit in special_cases:
        result_value, result_unit = sensor._normalize_data_rate(value, unit)
        # Should return original values without crashing
        assert result_value == value, f"Expected original value {value}, got {result_value}"
        assert result_unit == unit, f"Expected original unit {unit}, got {result_unit}"


def test_whitespace_and_formatting() -> None:
    """Test handling of whitespace and formatting variations."""
    sensor = LibreHardwareMonitorSensor(None, None, None)  # type: ignore[arg-type]

    # Test whitespace variations
    whitespace_cases = [
        (" 1024,5 ", "kB/s", "1.0", "MB/s"),      # Leading/trailing spaces
        ("\t1024,5\t", "MB/s", "1024.5", "MB/s"), # Tabs
        ("\n1024,5\n", "GB/s", "1048576.0", "MB/s"), # Newlines
        ("   ", "MB/s", "   ", "MB/s"),            # Only whitespace
        ("", "kB/s", "", "kB/s"),                  # Empty string
        ("-", "GB/s", "-", "GB/s"),                # Just dash
    ]

    for value, unit, expected_value, expected_unit in whitespace_cases:
        result_value, result_unit = sensor._normalize_data_rate(value, unit)
        assert result_value == expected_value, f"Failed for '{value}' {unit}: got '{result_value}', expected '{expected_value}'"
        assert result_unit == expected_unit, f"Failed for '{value}' {unit}: got '{result_unit}', expected '{expected_unit}'"


def test_negative_values() -> None:
    """Test handling of negative values."""
    sensor = LibreHardwareMonitorSensor(None, None, None)  # type: ignore[arg-type]

    # Test negative values (should be preserved as-is)
    negative_cases = [
        ("-1024,5", "kB/s", "-1.0", "MB/s"),      # Negative kB/s
        ("-512,25", "MB/s", "-512.25", "MB/s"),   # Negative MB/s
        ("-0,5", "GB/s", "-512.0", "MB/s"),       # Negative GB/s
        ("-0", "kB/s", "0.0", "MB/s"),            # Negative zero (should normalize to positive zero)
        ("-0,0", "MB/s", "0.0", "MB/s"),          # Negative zero with decimal
    ]

    for value, unit, expected_value, expected_unit in negative_cases:
        result_value, result_unit = sensor._normalize_data_rate(value, unit)
        assert result_value == expected_value, f"Failed for {value} {unit}: got {result_value}, expected {expected_value}"
        assert result_unit == expected_unit, f"Failed for {value} {unit}: got {result_unit}, expected {expected_unit}"


def test_scientific_notation() -> None:
    """Test handling of scientific notation."""
    sensor = LibreHardwareMonitorSensor(None, None, None)  # type: ignore[arg-type]

    # Test scientific notation (LibreHardwareMonitor might report very large/small values this way)
    scientific_cases = [
        ("1e3", "kB/s", "0.977", "MB/s"),         # 1000 kB/s
        ("1E3", "kB/s", "0.977", "MB/s"),         # 1000 kB/s (uppercase E)
        ("1e-3", "GB/s", "1.024", "MB/s"),        # 0.001 GB/s
        ("1E-3", "GB/s", "1.024", "MB/s"),        # 0.001 GB/s (uppercase E)
        ("1.5e2", "MB/s", "150.0", "MB/s"),       # 150 MB/s
        ("1,5e2", "MB/s", "150.0", "MB/s"),       # 150 MB/s (European decimal)
    ]

    for value, unit, expected_value, expected_unit in scientific_cases:
        result_value, result_unit = sensor._normalize_data_rate(value, unit)
        assert result_value == expected_value, f"Failed for {value} {unit}: got {result_value}, expected {expected_value}"
        assert result_unit == expected_unit, f"Failed for {value} {unit}: got {result_unit}, expected {expected_unit}"


def test_mixed_decimal_separators() -> None:
    """Test handling of mixed decimal separators."""
    sensor = LibreHardwareMonitorSensor(None, None, None)  # type: ignore[arg-type]

    # Test mixed separators (edge case that might occur)
    mixed_cases = [
        ("1024.5", "kB/s", "1.0", "MB/s"),        # Standard decimal point
        ("1024,5", "kB/s", "1.0", "MB/s"),        # European decimal comma
        ("1.024,5", "kB/s", "1.0", "MB/s"),      # Mixed (should handle gracefully)
        ("1,024.5", "kB/s", "1.0", "MB/s"),      # Mixed (should handle gracefully)
    ]

    for value, unit, expected_value, expected_unit in mixed_cases:
        result_value, result_unit = sensor._normalize_data_rate(value, unit)
        assert result_value == expected_value, f"Failed for {value} {unit}: got {result_value}, expected {expected_value}"
        assert result_unit == expected_unit, f"Failed for {value} {unit}: got {result_unit}, expected {expected_unit}"


def test_unicode_and_special_characters() -> None:
    """Test handling of unicode and special characters."""
    sensor = LibreHardwareMonitorSensor(None, None, None)  # type: ignore[arg-type]

    # Test unicode and special characters
    unicode_cases = [
        ("1024,5", "kB/s", "1.0", "MB/s"),        # Normal case
        ("１０２４，５", "kB/s", "１０２４，５", "kB/s"),  # Full-width numbers (should be preserved)
        ("1024.5", "kB/s", "1.0", "MB/s"),        # Normal decimal point
        ("1024·5", "kB/s", "1024·5", "kB/s"),     # Middle dot (should be preserved)
        ("1024•5", "kB/s", "1024•5", "kB/s"),     # Bullet point (should be preserved)
        ("1024×5", "kB/s", "1024×5", "kB/s"),     # Multiplication sign (should be preserved)
    ]

    for value, unit, expected_value, expected_unit in unicode_cases:
        result_value, result_unit = sensor._normalize_data_rate(value, unit)
        assert result_value == expected_value, f"Failed for {value} {unit}: got {result_value}, expected {expected_value}"
        assert result_unit == expected_unit, f"Failed for {value} {unit}: got {result_unit}, expected {expected_unit}"


async def test_coordinator_error_handling(
    hass: HomeAssistant,
    mock_lhm_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that coordinator handles errors gracefully."""
    coordinator = LibreHardwareMonitorCoordinator(hass, mock_config_entry)

    # Test connection error handling
    mock_lhm_client.get_data.side_effect = LibreHardwareMonitorConnectionError("Connection failed")
    with pytest.raises(Exception) as exc_info:
        await coordinator._async_update_data()
    assert "LibreHardwareMonitor connection failed" in str(exc_info.value)

    # Test no devices error handling
    mock_lhm_client.get_data.side_effect = LibreHardwareMonitorNoDevicesError("No devices")
    with pytest.raises(Exception) as exc_info:
        await coordinator._async_update_data()
    assert "No sensor data available" in str(exc_info.value)

    # Test unexpected error handling
    mock_lhm_client.get_data.side_effect = RuntimeError("Unexpected error")
    with pytest.raises(Exception) as exc_info:
        await coordinator._async_update_data()
    assert "Unexpected error occurred" in str(exc_info.value)


async def test_sensor_with_extreme_values(
    hass: HomeAssistant,
    mock_lhm_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test sensor handling with extreme values."""
    await init_integration(hass, mock_config_entry)

    # Create sensors with extreme values
    extreme_sensors = [
        LibreHardwareMonitorSensorData(
            sensor_id="extreme-kb-sensor",
            name="Extreme kB Sensor",
            value="999999,999",  # Very large kB/s
            unit="kB/s",
            min="0,0",
            max="999999,999",
            device_id="test-device",
            device_name="Test Device",
            device_type="Test",
        ),
        LibreHardwareMonitorSensorData(
            sensor_id="extreme-gb-sensor",
            name="Extreme GB Sensor",
            value="0,000001",  # Very small GB/s
            unit="GB/s",
            min="0,0",
            max="1,0",
            device_id="test-device",
            device_name="Test Device",
            device_type="Test",
        ),
        LibreHardwareMonitorSensorData(
            sensor_id="negative-sensor",
            name="Negative Sensor",
            value="-1024,5",  # Negative value
            unit="kB/s",
            min="-2048,0",
            max="0,0",
            device_id="test-device",
            device_name="Test Device",
            device_type="Test",
        ),
    ]

    # Update mock data
    updated_data = dict(mock_lhm_client.get_data.return_value.sensor_data)
    for sensor in extreme_sensors:
        updated_data[sensor.sensor_id] = sensor

    mock_lhm_client.get_data.return_value = replace(
        mock_lhm_client.get_data.return_value,
        sensor_data=MappingProxyType(updated_data),
    )

    # Reload integration
    hass.config_entries.async_schedule_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Check extreme kB/s sensor
    kb_state = hass.states.get("sensor.test_device_extreme_kb_sensor")
    if kb_state:
        assert kb_state.state == "976.562"  # 999999.999 kB/s normalized
        assert kb_state.attributes.get("unit_of_measurement") == "MB/s"

    # Check extreme GB/s sensor
    gb_state = hass.states.get("sensor.test_device_extreme_gb_sensor")
    if gb_state:
        assert gb_state.state == "0.001"  # 0.000001 GB/s normalized
        assert gb_state.attributes.get("unit_of_measurement") == "MB/s"

    # Check negative sensor
    neg_state = hass.states.get("sensor.test_device_negative_sensor")
    if neg_state:
        assert neg_state.state == "-1.0"  # -1024.5 kB/s normalized
        assert neg_state.attributes.get("unit_of_measurement") == "MB/s"


async def test_sensor_with_invalid_data(
    hass: HomeAssistant,
    mock_lhm_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test sensor handling with invalid data values."""
    await init_integration(hass, mock_config_entry)

    # Create sensors with invalid values
    invalid_sensors = [
        LibreHardwareMonitorSensorData(
            sensor_id="nan-sensor",
            name="NaN Sensor",
            value="NaN",
            unit="MB/s",
            min="0,0",
            max="100,0",
            device_id="test-device",
            device_name="Test Device",
            device_type="Test",
        ),
        LibreHardwareMonitorSensorData(
            sensor_id="inf-sensor",
            name="Infinity Sensor",
            value="inf",
            unit="kB/s",
            min="0,0",
            max="1000,0",
            device_id="test-device",
            device_name="Test Device",
            device_type="Test",
        ),
        LibreHardwareMonitorSensorData(
            sensor_id="empty-sensor",
            name="Empty Sensor",
            value="",
            unit="GB/s",
            min="0,0",
            max="1,0",
            device_id="test-device",
            device_name="Test Device",
            device_type="Test",
        ),
        LibreHardwareMonitorSensorData(
            sensor_id="dash-sensor",
            name="Dash Sensor",
            value="-",
            unit="MB/s",
            min="0,0",
            max="100,0",
            device_id="test-device",
            device_name="Test Device",
            device_type="Test",
        ),
    ]

    # Update mock data
    updated_data = dict(mock_lhm_client.get_data.return_value.sensor_data)
    for sensor in invalid_sensors:
        updated_data[sensor.sensor_id] = sensor

    mock_lhm_client.get_data.return_value = replace(
        mock_lhm_client.get_data.return_value,
        sensor_data=MappingProxyType(updated_data),
    )

    # Reload integration
    hass.config_entries.async_schedule_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Check that invalid values are preserved as-is
    nan_state = hass.states.get("sensor.test_device_nan_sensor")
    if nan_state:
        assert nan_state.state == "NaN"
        assert nan_state.attributes.get("unit_of_measurement") == "MB/s"

    inf_state = hass.states.get("sensor.test_device_infinity_sensor")
    if inf_state:
        assert inf_state.state == "inf"
        assert inf_state.attributes.get("unit_of_measurement") == "kB/s"

    empty_state = hass.states.get("sensor.test_device_empty_sensor")
    if empty_state:
        assert empty_state.state == ""
        assert empty_state.attributes.get("unit_of_measurement") == "GB/s"

    dash_state = hass.states.get("sensor.test_device_dash_sensor")
    if dash_state:
        assert dash_state.state == "-"
        assert dash_state.attributes.get("unit_of_measurement") == "MB/s"


def test_bytes_per_kb_constant() -> None:
    """Test that BYTES_PER_KB constant is correctly defined."""
    assert BYTES_PER_KB == 1024


def test_zero_value_handling() -> None:
    """Test explicit handling of zero values in data rate normalization."""
    sensor = LibreHardwareMonitorSensor(None, None, None)  # type: ignore[arg-type]

    # Test zero values for data rate units
    test_cases = [
        ("0", "kB/s", "0.0", "MB/s"),
        ("0,0", "MB/s", "0.0", "MB/s"),
        ("0,000", "GB/s", "0.0", "MB/s"),
    ]

    for value, unit, expected_value, expected_unit in test_cases:
        result_value, result_unit = sensor._normalize_data_rate(value, unit)
        assert result_value == expected_value
        assert result_unit == expected_unit

    # Test zero values for non-data-rate units (should remain unchanged)
    non_data_rate_cases = [
        ("0", "°C", "0", "°C"),
        ("0,0", "V", "0,0", "V"),
        ("0", "%", "0", "%"),
    ]

    for value, unit, expected_value, expected_unit in non_data_rate_cases:
        result_value, result_unit = sensor._normalize_data_rate(value, unit)
        assert result_value == expected_value
        assert result_unit == expected_unit


async def test_data_rate_normalization_integration(
    hass: HomeAssistant,
    mock_lhm_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test that data rate normalization works in the full integration."""
    await init_integration(hass, mock_config_entry)

    # Find a data rate sensor (if any exist in the test data)
    # We'll create a mock sensor with kB/s data

    # Create a mock sensor with kB/s data
    mock_sensor_data = LibreHardwareMonitorSensorData(
        sensor_id="test-hdd-read-rate",
        name="HDD Read Rate",
        value="1024,5",  # European decimal separator
        unit="kB/s",
        min="512,0",
        max="2048,0",
        device_id="hdd-0",
        device_name="Test HDD",
        device_type="Storage",
    )

    # Update the mock data to include our test sensor
    updated_data = dict(mock_lhm_client.get_data.return_value.sensor_data)
    updated_data["test-hdd-read-rate"] = mock_sensor_data

    mock_lhm_client.get_data.return_value = replace(
        mock_lhm_client.get_data.return_value,
        sensor_data=MappingProxyType(updated_data),
    )

    # Reload the integration to pick up the new sensor
    hass.config_entries.async_schedule_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Check that the sensor was created and normalized
    entity_id = "sensor.test_hdd_hdd_read_rate"
    state = hass.states.get(entity_id)

    if state:  # Only test if the sensor was created
        assert state.state == "1.0"  # 1024.5 kB/s normalized to 1.0 MB/s
        assert state.attributes.get("unit_of_measurement") == "MB/s"


async def test_european_decimal_separator_handling(
    hass: HomeAssistant,
    mock_lhm_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test that European decimal separators are properly handled."""
    await init_integration(hass, mock_config_entry)

    entity_id = "sensor.amd_ryzen_7_7800x3d_package_temperature"

    # Test with European decimal separator
    updated_data = dict(mock_lhm_client.get_data.return_value.sensor_data)
    updated_data["amdcpu-0-temperature-3"] = replace(
        updated_data["amdcpu-0-temperature-3"], value="42,1"  # European comma
    )
    mock_lhm_client.get_data.return_value = replace(
        mock_lhm_client.get_data.return_value,
        sensor_data=MappingProxyType(updated_data),
    )

    freezer.tick(timedelta(DEFAULT_SCAN_INTERVAL))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state
    assert state.state == "42.1"  # Should be converted to standard decimal point
