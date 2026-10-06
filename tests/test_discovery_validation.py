"""Discovery remains broad and local, preceding the pairing-mode prompt."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import hashlib

from Crypto.Cipher import AES
import pytest
from custom_components.tuya_ble.config_flow import (
    TuyaBLEConfigFlow,
    _has_tuya_service_data,
)
from custom_components.tuya_ble.tuya_ble import SERVICE_UUIDS


def advertisement(service_data=None, name="TY", manufacturer_data=None):
    return SimpleNamespace(
        address="AA:BB:CC:DD:EE:01",
        name=name,
        device=SimpleNamespace(name=name),
        service_data=service_data,
        service_uuids=list(SERVICE_UUIDS),
        manufacturer_data=manufacturer_data or {},
    )


def locally_pairable_advertisement(product=b"newmodel", bound=False):
    key = hashlib.md5(product).digest()
    manufacturer = (
        bytes([0x80 if bound else 0, 3, 0, 0, 0, 0])
        + AES.new(key, AES.MODE_CBC, key).encrypt(b"testidentity0001")
    )
    return advertisement(
        {SERVICE_UUIDS[0]: b"\0" + product}, manufacturer_data={2000: manufacturer}
    )


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {SERVICE_UUIDS[0]: b""},
        {SERVICE_UUIDS[0]: b"\0"},
        {SERVICE_UUIDS[0]: b"\2unrelated"},
        {"unrelated": b"data"},
    ],
)
async def test_invalid_advertisement_rejected_before_flow_identity(data):
    flow = TuyaBLEConfigFlow()
    flow.async_set_unique_id = AsyncMock()
    flow.async_abort = Mock(return_value={"reason": "not_supported"})
    assert (await flow.async_step_bluetooth(advertisement(data)))[
        "reason"
    ] == "not_supported"
    flow.async_set_unique_id.assert_not_awaited()


async def test_unknown_unbound_product_discovers_before_pairing_mode():
    flow = TuyaBLEConfigFlow()
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    flow.context = {}
    flow.async_show_form = Mock(return_value={"type": "form"})
    with patch(
        "custom_components.tuya_ble.config_flow.pair_local", new_callable=AsyncMock
    ) as pair:
        result = await flow.async_step_bluetooth(
            locally_pairable_advertisement(b"futuremodel")
        )
        assert result["type"] == "form"
        pair.assert_not_awaited()
    assert flow.async_show_form.call_args.kwargs["step_id"] == "local_pair"


def test_bound_or_malformed_vendor_advertisements_are_rejected():
    assert not _has_tuya_service_data(locally_pairable_advertisement(bound=True))
    malformed = advertisement(
        {SERVICE_UUIDS[0]: b"\0newmodel"}, manufacturer_data={2000: b"\0" * 22}
    )
    assert not _has_tuya_service_data(malformed)


def test_dryer_and_uuid_only_are_rejected():
    dryer = advertisement({}, "Dryer")
    dryer.manufacturer_data = {0x75: bytes.fromhex("421f3001010f00f0f10100")}
    assert not _has_tuya_service_data(dryer)
    assert not _has_tuya_service_data(advertisement())
    assert _has_tuya_service_data(locally_pairable_advertisement())
    assert not _has_tuya_service_data(advertisement({SERVICE_UUIDS[0]: b"\2data"}))


def test_manual_discovery_filters_and_keeps_existing_entries():
    good = locally_pairable_advertisement(b"future")
    bad = advertisement({}, "Dryer")
    bad.address = "other"
    flow = TuyaBLEConfigFlow()
    flow.hass = Mock()
    flow._async_current_ids = Mock(return_value=set())
    with patch(
        "custom_components.tuya_ble.config_flow.async_discovered_service_info",
        return_value=[bad, good, good],
    ):
        flow._collect_discovered_devices()
    assert list(flow._discovered_devices) == [good.address]
    flow._discovered_devices.clear()
    flow._async_current_ids.return_value = {good.address}
    with patch(
        "custom_components.tuya_ble.config_flow.async_discovered_service_info",
        return_value=[good],
    ):
        flow._collect_discovered_devices()
    assert not flow._discovered_devices


async def test_discovery_names_known_product_without_pairing_or_credentials():
    from custom_components.tuya_ble.devices import get_device_readable_name

    discovery = locally_pairable_advertisement(b"gvygg3m8")
    assert (
        await get_device_readable_name(discovery, None) == "SGS01 Plant Sensor DDEE01"
    )
    flow = TuyaBLEConfigFlow()
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    flow.context = {}
    flow.async_show_form = Mock()
    await flow.async_step_bluetooth(discovery)
    assert flow.context["title_placeholders"]["name"] == "SGS01 Plant Sensor DDEE01"


@pytest.mark.parametrize("payload", [b"\0newmodel", b"\1gvygg3m8", b"\0\xff"])
async def test_unrecognized_product_keeps_generic_discovery_name(payload):
    from custom_components.tuya_ble.devices import get_device_readable_name

    discovery = advertisement({SERVICE_UUIDS[0]: payload})
    assert await get_device_readable_name(discovery, None) == "Tuya BLE Device DDEE01"
    discovery.name = "Garden Sensor"
    assert await get_device_readable_name(discovery, None) == "Garden Sensor DDEE01"


async def test_name_survives_bound_advertisement_and_restart(hass):
    from custom_components.tuya_ble.devices import get_device_readable_name
    from homeassistant.helpers.storage import Store

    discovery = advertisement({SERVICE_UUIDS[0]: b"\0gvygg3m8"})
    assert (
        await get_device_readable_name(discovery, None, hass)
        == "SGS01 Plant Sensor DDEE01"
    )
    # Actual bound spare payload, with no manufacturer data in the passive report.
    discovery.service_data = {SERVICE_UUIDS[0]: bytes.fromhex("00cd2095200e55487f")}
    assert await Store(hass, 1, "tuya_ble.discovery.aabbccddee01").async_load() == {
        "name": "SGS01 Plant Sensor"
    }
    assert (
        await get_device_readable_name(discovery, None, hass)
        == "SGS01 Plant Sensor DDEE01"
    )


async def test_bound_name_uses_saved_local_pairing_product(hass):
    from custom_components.tuya_ble.devices import get_device_readable_name
    from homeassistant.helpers.storage import Store

    await Store(
        hass, 1, "tuya_ble.local_pairing.aabbccddee01", private=True
    ).async_save({"product_id": "gvygg3m8"})
    discovery = advertisement({SERVICE_UUIDS[0]: bytes.fromhex("00cd2095200e55487f")})
    assert (
        await get_device_readable_name(discovery, None, hass)
        == "SGS01 Plant Sensor DDEE01"
    )


async def test_pending_card_updates_from_later_product_advertisement(hass):
    flow = TuyaBLEConfigFlow()
    flow.hass = hass
    flow.context = {"title_placeholders": {"name": "Tuya BLE Device DDEE01"}}
    flow._pending_address = "AA:BB:CC:DD:EE:01"
    remove = Mock()
    with patch(
        "custom_components.tuya_ble.config_flow.async_register_callback",
        return_value=remove,
    ) as register:
        flow._watch_discovery_name()
    listener = register.call_args.args[1]
    listener(advertisement({SERVICE_UUIDS[0]: b"\0gvygg3m8"}), None)
    await flow._name_task
    assert flow.context["title_placeholders"]["name"] == "SGS01 Plant Sensor DDEE01"
    # Opaque reports must not replace the automatically learned model name.
    listener(
        advertisement({SERVICE_UUIDS[0]: bytes.fromhex("00cd2095200e55487f")}), None
    )
    await flow._name_task
    assert flow.context["title_placeholders"]["name"] == "SGS01 Plant Sensor DDEE01"
    flow.async_remove()
    remove.assert_called_once()
