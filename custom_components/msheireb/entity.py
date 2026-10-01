"""Base entity and device helpers."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN, INTEGRATION_VERSION
from .coordinator import ContractData, MsheirebCoordinator
from .models import HvacZone


def apartment_device(contract_id: int, title: str) -> DeviceInfo:
    """The apartment (one per contract)."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"contract_{contract_id}")},
        name=f"Msheireb {title}",
        manufacturer="Msheireb Properties",
        model="Smart apartment",
        sw_version=INTEGRATION_VERSION,
    )


def room_name(zone: HvacZone, disambiguate: bool = False) -> str:
    return f"{zone.room_name} {zone.device_name}" if disambiguate else zone.room_name


def room_device(zone: HvacZone, disambiguate: bool = False) -> DeviceInfo:
    """One device per room HVAC zone, attached to the apartment."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"zone_{zone.key}")},
        name=room_name(zone, disambiguate),
        manufacturer="Msheireb Properties",
        model="Room HVAC",
        suggested_area=zone.room_name,
        via_device=(DOMAIN, f"contract_{zone.contract_id}"),
        sw_version=INTEGRATION_VERSION,
    )


def room_entity_id(domain: str, cd: ContractData, zone: HvacZone, suffix: str = "") -> str:
    """Stable default entity_id (msheireb_<unit>_<room>[_suffix]); the registry keeps existing ids."""
    return f"{domain}." + slugify(f"msheireb {cd.title} {room_name(zone, zone_needs_disambiguation(cd, zone))} {suffix}")


def zone_needs_disambiguation(cd: ContractData, zone: HvacZone) -> bool:
    return [z.room_name for z in cd.zones.values()].count(zone.room_name) > 1


class MsheirebEntity(CoordinatorEntity[MsheirebCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: MsheirebCoordinator, contract_id: int) -> None:
        super().__init__(coordinator)
        self._contract_id = contract_id
        cd = self.contract_data
        self._attr_device_info = apartment_device(contract_id, cd.title if cd else str(contract_id))

    @property
    def contract_data(self) -> ContractData | None:
        return (self.coordinator.data or {}).get(self._contract_id)
