"""v0.3.18: fan mode 'off' in the climate fan list (Off = room off; a speed while off = on at that speed)."""
from .fake_api import FakeApi  # noqa: F401
from .test_off_fan_memory import (  # noqa: F401
    AUTO,
    DINING,
    HIGH,
    KEY,
    LOW,
    MED,
    POWER,
    _digital,
    _poll,
    _set_fan_at_panel,
    _setup,
    _sns,
    _svc,
    fake_api,
)


def _attrs(hass):
    s = hass.states.get(DINING)
    return s.state, s.attributes.get("fan_mode")


async def test_fan_modes_list_has_off_first(hass):
    await _setup(hass)
    assert hass.states.get(DINING).attributes["fan_modes"] == ["off", "low", "medium", "high", "auto"]


async def test_fan_off_turns_room_off_like_hvac_off(hass):
    entry, coord, api = await _setup(hass)  # fixture: on, fan high
    await _svc(hass, "set_fan_mode", fan_mode="off")
    assert _sns(api) == [POWER, AUTO]  # same as HVAC Off: power, then fan Auto
    assert _attrs(hass) == ("off", "off")
    assert coord.drift.prev_fan[KEY] == "high"  # speed remembered
    assert coord.drift.desired[KEY] == {"power": False, "fan": "auto"}  # never fan 'off'
    await _poll(hass, coord)
    assert coord.health.commands[KEY].result == "confirmed"
    assert _attrs(hass) == ("off", "off")


async def test_fan_off_with_fan_auto_when_off_disabled(hass):
    entry, coord, api = await _setup(hass)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "fan_auto_when_off": False})
    await hass.async_block_till_done()
    coord, api = entry.runtime_data, FakeApi.instances[-1]
    await _svc(hass, "set_fan_mode", fan_mode="off")
    assert _sns(api) == [POWER]  # power only, like HVAC Off with the option disabled
    assert coord.drift.desired[KEY] == {"power": False, "fan": "high"}
    assert _attrs(hass) == ("off", "off")


async def test_speed_while_off_turns_on_at_that_speed(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "turn_off")
    await _poll(hass, coord)
    api.commands.clear()
    await _svc(hass, "set_fan_mode", fan_mode="low")
    assert _sns(api) == [POWER, LOW]
    assert _attrs(hass) == ("cool", "low")
    assert coord.drift.prev_fan[KEY] == "low"  # the chosen speed is the saved one (not High)
    assert coord.drift.desired[KEY] == {"power": True, "fan": "low"}
    await _poll(hass, coord)
    assert coord.health.commands[KEY].result == "confirmed"
    assert _attrs(hass) == ("cool", "low")
    # next off/on cycle via HVAC restores the new saved speed
    await _svc(hass, "turn_off"); await _poll(hass, coord)
    api.commands.clear()
    await _svc(hass, "set_hvac_mode", hvac_mode="cool")
    assert _sns(api) == [POWER, LOW]


async def test_speed_while_on_unchanged(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="medium")
    assert _sns(api) == [MED]  # no power press
    assert _attrs(hass) == ("cool", "medium")
    assert coord.drift.desired[KEY] == {"fan": "medium"}


async def test_fan_mode_off_when_off_outside_ha(hass):
    entry, coord, api = await _setup(hass)
    assert _attrs(hass) == ("cool", "high")
    _digital(api)["HVAC AC"]["current_status"] = "OFF"  # wall panel
    await _poll(hass, coord)
    assert _attrs(hass) == ("off", "off")
    _digital(api)["HVAC AC"]["current_status"] = "ON"
    _set_fan_at_panel(api, "HVAC Fan Medium")
    await _poll(hass, coord)
    assert _attrs(hass) == ("cool", "medium")


async def test_hvac_cool_still_restores_saved_speed(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="off")
    await _poll(hass, coord)
    api.commands.clear()
    await _svc(hass, "set_hvac_mode", hvac_mode="cool")
    assert _sns(api) == [POWER, HIGH]
    assert _attrs(hass) == ("cool", "high")


async def test_latest_action_wins_between_fan_off_and_speed(hass):
    entry, coord, api = await _setup(hass)
    FakeApi.ignore_commands = True  # presses never register: commands stay pending
    await _svc(hass, "set_fan_mode", fan_mode="off")
    assert _attrs(hass) == ("off", "off")  # optimistic
    await _svc(hass, "set_fan_mode", fan_mode="medium")
    assert _attrs(hass) == ("cool", "medium")  # the newer request is shown
    assert coord.drift.desired[KEY]["fan"] == "medium" and coord.drift.desired[KEY]["power"] is True


async def test_desired_fan_never_off(hass, hass_storage):
    from custom_components.msheireb.drift import store_key

    hass_storage[store_key("e1")] = {"version": 1, "minor_version": 1, "key": store_key("e1"),
                                    "data": {"desired": {KEY: {"fan": "off", "power": True}},
                                             "prev_fan": {KEY: "off"}}}
    entry, coord, api = await _setup(hass)
    assert "fan" not in coord.drift.desired[KEY] and KEY not in coord.drift.prev_fan  # cleaned on load
    coord.drift.set_desired(KEY, {"fan": "off"})
    assert coord.drift.desired[KEY] == {"power": False}  # Off maps to desired power off
    coord.drift.remember_fan(KEY, "off")
    assert KEY not in coord.drift.prev_fan


async def test_no_false_drift_while_off(hass):
    entry, coord, api = await _setup(hass, None)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "drift_grace": 0})
    await hass.async_block_till_done()
    coord, api = entry.runtime_data, FakeApi.instances[-1]
    await _svc(hass, "set_fan_mode", fan_mode="off")
    await _poll(hass, coord)
    api.commands.clear()
    for _ in range(3):
        await _poll(hass, coord)
    assert _attrs(hass) == ("off", "off")
    assert api.commands == [] and KEY not in coord.drift.episodes and coord.drift.events == 0
    # the fan (Auto) changed at the panel while off: still no drift (fan irrelevant while off)
    _set_fan_at_panel(api, "HVAC Fan Low")
    for _ in range(3):
        await _poll(hass, coord)
    assert api.commands == [] and coord.drift.events == 0
