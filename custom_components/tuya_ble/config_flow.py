"""Local-only setup and options for Tuya BLE."""

from __future__ import annotations
from typing import Any
import json
from bleak.exc import BleakError
import voluptuous as vol
from homeassistant.config_entries import (
    EVENT_FLOW_DISCOVERED,
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    async_register_callback,
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.const import CONF_ADDRESS, CONF_DEVICE_ID
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from .const import (
    CONF_UUID,
    CONF_LOCAL_KEY,
    CONF_LOCAL_KEY_HEX,
    CONF_SEC_KEY,
    CONF_CATEGORY,
    CONF_PRODUCT_ID,
    CONF_DEVICE_NAME,
    CONF_PRODUCT_MODEL,
    CONF_PRODUCT_NAME,
    CONF_FUNCTIONS,
    CONF_STATUS_RANGE,
    CONF_KEEP_CONNECTION,
    CONF_IDLE_DISCONNECT_DELAY,
    DEFAULT_KEEP_CONNECTION,
    DEFAULT_IDLE_DISCONNECT_DELAY,
    DOMAIN,
)
from .devices import devices_database, get_device_readable_name
from .local_pairing import LocalPairingError, decode_identity, pair_local
from .local_manager import local_options
from .schema import parse_schema
from .tuya_ble.security import TuyaBLESecurityMaterial


def _has_tuya_service_data(discovery: BluetoothServiceInfoBleak) -> bool:
    """Return whether this is an unbound device supported by local setup.

    A Tuya service UUID and a format byte identify a vendor advertisement, but
    they do not establish that the device is available to be adopted.  In
    particular, bound devices advertise the same framing and cannot accept the
    local provisioning exchange.  Require the complete protocol-3 identity
    record and reject already-bound devices before Home Assistant creates a
    discovery card.

    This deliberately does not use a product allowlist: an unknown product
    with a valid unbound local-pairing identity is still discovered.
    """
    identity = decode_identity(
        discovery.manufacturer_data or {}, discovery.service_data or {}
    )
    return (
        identity is not None
        and not identity["bound"]
        and identity["protocol_major"] == 3
    )


def _manual_schema(
    defaults: dict[str, Any], address_choices: dict[str, str] | None
) -> vol.Schema:
    """Schema for entering device credentials manually."""
    fields: dict[Any, Any] = {}
    if address_choices:
        fields[
            vol.Required(
                CONF_ADDRESS,
                default=defaults.get(CONF_ADDRESS, next(iter(address_choices))),
            )
        ] = vol.In(address_choices)
    fields[vol.Required(CONF_UUID, default=defaults.get(CONF_UUID, ""))] = str
    fields[vol.Optional(CONF_LOCAL_KEY, default=defaults.get(CONF_LOCAL_KEY, ""))] = (
        TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
    )
    fields[vol.Optional(CONF_SEC_KEY, default=defaults.get(CONF_SEC_KEY, ""))] = (
        TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
    )
    fields[
        vol.Optional(CONF_LOCAL_KEY_HEX, default=defaults.get(CONF_LOCAL_KEY_HEX, ""))
    ] = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
    fields[vol.Required(CONF_DEVICE_ID, default=defaults.get(CONF_DEVICE_ID, ""))] = str
    fields[vol.Required(CONF_PRODUCT_ID, default=defaults.get(CONF_PRODUCT_ID, ""))] = (
        str
    )
    fields[
        vol.Required(CONF_CATEGORY, default=defaults.get(CONF_CATEGORY, "szjqr"))
    ] = str
    fields[
        vol.Optional(CONF_DEVICE_NAME, default=defaults.get(CONF_DEVICE_NAME, ""))
    ] = str
    return vol.Schema(fields)


def _validate_manual(
    user_input: dict[str, Any], errors: dict[str, str]
) -> dict[str, Any] | None:
    """Validate manual credentials; return an options dict or None."""
    uuid = user_input[CONF_UUID].strip()
    local_key = (user_input.get(CONF_LOCAL_KEY) or "").strip()
    local_key_hex = (user_input.get(CONF_LOCAL_KEY_HEX) or "").strip()
    sec_key = (user_input.get(CONF_SEC_KEY) or "").strip()
    device_id = user_input[CONF_DEVICE_ID].strip()
    product_id = user_input[CONF_PRODUCT_ID].strip()
    category = user_input[CONF_CATEGORY].strip()

    if len(uuid) < 8:
        errors[CONF_UUID] = "invalid_uuid"
    try:
        if local_key and local_key_hex:
            raise ValueError("Use one key representation")
        TuyaBLESecurityMaterial(local_key, sec_key or None, local_key_hex or None)
    except ValueError:
        errors[CONF_LOCAL_KEY] = "invalid_local_key"
    if not device_id:
        errors[CONF_DEVICE_ID] = "invalid_device_id"
    if not product_id:
        errors[CONF_PRODUCT_ID] = "invalid_product_id"
    if not category:
        errors[CONF_CATEGORY] = "invalid_category"
    if errors:
        return None

    result = {
        CONF_UUID: uuid,
        CONF_LOCAL_KEY: local_key,
        CONF_DEVICE_ID: device_id,
        CONF_PRODUCT_ID: product_id,
        CONF_CATEGORY: category,
        CONF_DEVICE_NAME: (user_input.get(CONF_DEVICE_NAME) or "").strip()
        or product_id,
        CONF_PRODUCT_NAME: "",
        CONF_PRODUCT_MODEL: "",
    }
    if sec_key:
        result[CONF_SEC_KEY] = sec_key
    if local_key_hex:
        result[CONF_LOCAL_KEY_HEX] = local_key_hex
    return result


def _settings_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Schema for the connection policy."""
    return vol.Schema(
        {
            vol.Required(
                CONF_KEEP_CONNECTION,
                default=defaults.get(CONF_KEEP_CONNECTION, DEFAULT_KEEP_CONNECTION),
            ): bool,
            vol.Required(
                CONF_IDLE_DISCONNECT_DELAY,
                default=defaults.get(
                    CONF_IDLE_DISCONNECT_DELAY, DEFAULT_IDLE_DISCONNECT_DELAY
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=3600)),
        }
    )


class TuyaBLEOptionsFlow(OptionsFlowWithConfigEntry):
    """Edit local credentials and connection policy."""

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "manual", "schema"]
        )

    async def async_step_schema(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                parse_schema(user_input["schema"])
            except (ValueError, TypeError):
                errors["schema"] = "invalid_schema"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        **local_options(self.config_entry.options),
                        "schema": json.loads(user_input["schema"]),
                    },
                )
        defaults = self.config_entry.options.get("schema", [])
        return self.async_show_form(
            step_id="schema",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "schema",
                        default=(user_input or {}).get(
                            "schema", json.dumps(defaults, ensure_ascii=False, indent=2)
                        ),
                    ): TextSelector(TextSelectorConfig(multiline=True)),
                }
            ),
            errors=errors,
        )

    async def async_step_settings(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={**local_options(self.config_entry.options), **user_input},
            )
        return self.async_show_form(
            step_id="settings", data_schema=_settings_schema(self.config_entry.options)
        )

    async def async_step_manual(self, user_input=None):
        errors = {}
        if user_input is not None:
            credentials = _validate_manual(user_input, errors)
            if credentials:
                options = local_options(self.config_entry.options)
                options.pop(CONF_SEC_KEY, None)
                options.pop(CONF_LOCAL_KEY_HEX, None)
                options.update(credentials)
                return self.async_create_entry(title="", data=options)
        return self.async_show_form(
            step_id="manual",
            data_schema=_manual_schema(
                user_input or dict(self.config_entry.options), None
            ),
            errors=errors,
        )


class TuyaBLEConfigFlow(ConfigFlow, domain=DOMAIN):
    """Discover first; ask for pairing mode only after Add."""

    VERSION = 2

    def __init__(self):
        super().__init__()
        self._discovery_info = None
        self._discovered_devices = {}
        self._pending_address = None
        self._name_unsubscribe = None
        self._name_task = None

    async def async_step_bluetooth(self, discovery_info):
        if not _has_tuya_service_data(discovery_info):
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self._pending_address = discovery_info.address
        self.context["title_placeholders"] = {
            "name": await get_device_readable_name(discovery_info, None, self.hass)
        }
        self._watch_discovery_name()
        return await self.async_step_local_pair()

    def _watch_discovery_name(self):
        """Update a pending card when later advertisements identify the model."""
        if self.hass is None or self._name_unsubscribe is not None:
            return

        @callback
        def discovered(info, change):
            if self._name_task is not None and not self._name_task.done():
                return
            self._name_task = self.hass.async_create_task(
                self._refresh_discovery_name(info)
            )

        self._name_unsubscribe = async_register_callback(
            self.hass,
            discovered,
            {"address": self._pending_address},
            BluetoothScanningMode.PASSIVE,
        )

    async def _refresh_discovery_name(self, info):
        name = await get_device_readable_name(info, None, self.hass)
        if self.context.get("title_placeholders", {}).get("name") != name:
            self.context["title_placeholders"] = {"name": name}
            self.hass.bus.async_fire(EVENT_FLOW_DISCOVERED)

    @callback
    def async_remove(self):
        if self._name_unsubscribe is not None:
            self._name_unsubscribe()
            self._name_unsubscribe = None
        if self._name_task is not None:
            self._name_task.cancel()
        super().async_remove()

    async def async_step_user(self, user_input=None):
        return self.async_show_menu(
            step_id="user", menu_options=["local_device", "manual"]
        )

    async def async_step_manual(self, user_input=None):
        self._collect_discovered_devices()
        if not self._discovered_devices:
            return self.async_abort(reason="no_unconfigured_devices")
        errors = {}
        if user_input is not None:
            credentials = _validate_manual(user_input, errors)
            if credentials:
                address = user_input[CONF_ADDRESS]
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=credentials[CONF_DEVICE_NAME],
                    data={CONF_ADDRESS: address},
                    options={CONF_ADDRESS: address, **credentials},
                )
        choices = {
            a: await get_device_readable_name(i, None, self.hass)
            for a, i in self._discovered_devices.items()
        }
        return self.async_show_form(
            step_id="manual",
            data_schema=_manual_schema(user_input or {}, choices),
            errors=errors,
        )

    async def async_step_local_device(self, user_input=None) -> FlowResult:
        """Choose a discovered device without requiring pairing mode yet."""
        if self._discovery_info:
            self._pending_address = self._discovery_info.address
            return await self.async_step_local_pair()
        self._collect_discovered_devices()
        if not self._discovered_devices:
            return self.async_abort(reason="no_unconfigured_devices")
        if user_input is not None:
            self._pending_address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(self._pending_address)
            self._abort_if_unique_id_configured()
            return await self.async_step_local_pair()
        return self.async_show_form(
            step_id="local_device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            a: await get_device_readable_name(i, None, self.hass)
                            for a, i in self._discovered_devices.items()
                        }
                    )
                }
            ),
        )

    async def async_step_local_pair(self, user_input=None) -> FlowResult:
        """Ask for pairing mode after discovery, then provision locally."""
        errors = {}
        if user_input is not None:
            try:
                saved = await pair_local(self.hass, self._pending_address)
            except LocalPairingError as err:
                errors["base"] = str(err)
            except (BleakError, TimeoutError):
                errors["base"] = "local_cannot_connect"
            else:
                category = next(
                    (
                        name
                        for name, info in devices_database.items()
                        if saved["product_id"] in info.products
                    ),
                    "",
                )
                product = (
                    devices_database[category].products[saved["product_id"]]
                    if category
                    else None
                )
                title = f"{product.name if product else 'Tuya BLE'} {self._pending_address[-8:].replace(':', '')}"
                options = {
                    CONF_ADDRESS: self._pending_address,
                    CONF_UUID: saved["uuid"],
                    CONF_LOCAL_KEY: "",
                    CONF_LOCAL_KEY_HEX: saved["local_key_hex"],
                    CONF_DEVICE_ID: saved["device_id"],
                    CONF_PRODUCT_ID: saved["product_id"],
                    CONF_CATEGORY: category,
                    CONF_DEVICE_NAME: title,
                    CONF_PRODUCT_NAME: product.name if product else saved["product_id"],
                    CONF_PRODUCT_MODEL: "",
                    CONF_FUNCTIONS: [],
                    CONF_STATUS_RANGE: [],
                }
                return self.async_create_entry(
                    title=title,
                    data={CONF_ADDRESS: self._pending_address},
                    options=options,
                )
        if errors.get("base") in {
            "local_identity_missing",
            "local_cannot_connect",
            "local_verification_failed",
        }:
            return await self.async_step_local_retry()
        return self.async_show_form(
            step_id="local_pair",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"address": self._pending_address},
        )

    async def async_step_local_retry(self, user_input=None):
        return self.async_show_menu(
            step_id="local_retry", menu_options=["retry_pair", "cancel"]
        )

    async def async_step_retry_pair(self, user_input=None):
        return await self.async_step_local_pair({})

    async def async_step_cancel(self, user_input=None):
        return self.async_abort(reason="pairing_cancelled")

    def _collect_discovered_devices(self) -> None:
        """Collect connectable, not yet configured Tuya BLE devices."""
        if discovery := self._discovery_info:
            self._discovered_devices[discovery.address] = discovery
            return
        current_addresses = self._async_current_ids()
        for discovery in async_discovered_service_info(self.hass):
            if (
                discovery.address in current_addresses
                or discovery.address in self._discovered_devices
                or not _has_tuya_service_data(discovery)
            ):
                continue
            self._discovered_devices[discovery.address] = discovery

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> TuyaBLEOptionsFlow:
        """Get the options flow for this handler."""
        return TuyaBLEOptionsFlow(config_entry)
