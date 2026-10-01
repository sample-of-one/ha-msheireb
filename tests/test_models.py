from custom_components.msheireb.models import parse_smart_home


def test_parse_real_payload(smart_home_payload):
    zones = parse_smart_home(4242, smart_home_payload)
    assert [z.room_name for z in sorted(zones.values(), key=lambda z: z.sort_key)] == [
        "Dining Room", "Master Bedroom", "Bedroom 1"]
    dining = zones["4242_501"]
    assert {r: c.sn for r, c in dining.controls.items()} == {
        "power": 1, "auto": 2, "low": 3, "medium": 4, "high": 5,
        "temp_up": 6, "temp_down": 7, "setpoint": 1}
    assert dining.controls["temp_up"].type_code == "D"
    assert dining.controls["setpoint"].type_code == "A"
    assert dining.setpoint == 19.5 and dining.room_temperature == 21.5
    assert dining.power is True
    assert dining.fan_mode(("auto", "low", "medium", "high")) == "high"
    master = zones["4242_502"]
    assert master.controls["temp_down"].sn == 14 and master.controls["power"].sn == 8
    assert master.room_temperature == 19.0
