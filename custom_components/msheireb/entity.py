"""Base entity."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ContractData, MsheirebCoordinator


class MsheirebEntity(CoordinatorEntity[MsheirebCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: MsheirebCoordinator, contract_id: int) -> None:
        super().__init__(coordinator)
        self._contract_id = contract_id
        cd = self.contract_data
        title = cd.title if cd else str(contract_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"contract_{contract_id}")},
            name=f"Msheireb {title}",
            manufacturer="Msheireb Properties",
            model="Smart apartment",
        )

    @property
    def contract_data(self) -> ContractData | None:
        return (self.coordinator.data or {}).get(self._contract_id)
