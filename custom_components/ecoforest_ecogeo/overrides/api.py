import asyncio
import json
import logging
import re
import string
from pathlib import Path

import httpx
from pyecoforest.api import EcoforestApi

from custom_components.ecoforest_ecogeo.overrides.device import EcoGeoDevice

_LOGGER = logging.getLogger(__name__)

MODEL_ADDRESS = 5323
MODEL_LENGTH = 6


class DataTypes:
    Register = 1
    Coil = 2


class Operations:
    Get = {DataTypes.Coil: 2001, DataTypes.Register: 2002}


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _infer_entity_type(name: str) -> str:
    n = name.lower()
    if "temperature" in n:
        return "temperature"
    if "pressure" in n:
        return "pressure"
    POWER_TERMS = ["power", "capacity", "PF", "cop", "consumption", "production"]
    if any(term.lower() in n.lower() for term in POWER_TERMS):
        return "power"
    return "measurement"


def _build_mapping(block: dict) -> dict:
    mapping: dict[str, dict] = {}
    for item in block.get("analog", []):
        key = _slugify(item["name"])
        mapping[key] = {
            "data_type": DataTypes.Register,
            "type": "float",
            "address": item["address"],
            "entity_type": _infer_entity_type(item["name"]),
        }

    # Some integers are actually floats, depends on the inferred entity type for now
    for item in block.get("integer", []):
        inferred_type = _infer_entity_type(item["name"])
        if inferred_type in ("power", "temperature"):
            type = "float"
        else:
            type = "int"
        key = _slugify(item["name"])
        mapping[key] = {
            "data_type": DataTypes.Register,
            "type": type,
            "address": item["address"],
            "entity_type": inferred_type,
        }
    for item in block.get("boolean", []):
        key = _slugify(item["name"])
        mapping[key] = {
            "data_type": DataTypes.Coil,
            "type": "boolean",
            "address": item["address"],
            "entity_type": "measurement",
        }
    return mapping


def _build_requests(mapping: dict) -> dict:
    requests = {DataTypes.Register: [], DataTypes.Coil: []}
    for dt in [DataTypes.Register, DataTypes.Coil]:
        addresses = sorted(
            [d["address"] for d in mapping.values() if d["data_type"] == dt]
        )
        if not addresses:
            continue
        start = prev = addresses[0]
        for addr in addresses[1:]:
            if addr != prev + 1:
                requests[dt].append({"address": start, "length": prev - start + 1})
                start = addr
            prev = addr
        requests[dt].append({"address": start, "length": prev - start + 1})
    return requests


# Load modbus information and build mappings for domestic and HP models
with open(
    Path(__file__).resolve().parent.parent / "modbus_info.json", "r", encoding="utf-8"
) as f:
    _info = json.load(f)
_DOMESTIC_MAPPING = _build_mapping(_info["ecoGEO_domestic"])
_HP_MAPPING = _build_mapping(_info["ecoGEO_HP"])
_DOMESTIC_REQUESTS = _build_requests(_DOMESTIC_MAPPING)
_HP_REQUESTS = _build_requests(_HP_MAPPING)

MAPPING = _DOMESTIC_MAPPING

HP_MODELS = {"EATM00"}

# ---------------------------------------------------------------------------
# API implementation
# ---------------------------------------------------------------------------


class EcoGeoApi(EcoforestApi):
    def __init__(self, host: str, user: str, password: str) -> None:
        super().__init__(host, httpx.BasicAuth(user, password))
        self._MAPPING = MAPPING
        self._REQUESTS = _DOMESTIC_REQUESTS
        self._model_name: str | None = None

    async def get(self) -> EcoGeoDevice:
        state = {DataTypes.Coil: {}, DataTypes.Register: {}}

        if self._model_name is None:
            model_data = await self._load_data(
                MODEL_ADDRESS, MODEL_LENGTH, Operations.Get[DataTypes.Register]
            )
            print(f"model_data: {model_data}")
            model_dictionary = ["--"] + [*string.digits] + [*string.ascii_uppercase]
            self._model_name = "".join(
                [
                    model_dictionary[self.parse_ecoforest_int(x)]
                    for x in model_data.values()
                ]
            )
            print(f"model_name: {self._model_name}")

            if self._model_name in HP_MODELS:
                print("HP MODELS")
                self._MAPPING = _HP_MAPPING
                self._REQUESTS = _HP_REQUESTS
            else:
                print("DOMESTIC MODELS")
                self._MAPPING = _DOMESTIC_MAPPING
                self._REQUESTS = _DOMESTIC_REQUESTS
            global MAPPING
            MAPPING = self._MAPPING

        for dt in [DataTypes.Coil, DataTypes.Register]:
            for request in self._REQUESTS[dt]:
                print(f"Getting: addr:{request['address']} len:{request['length']}")
                # Retry with backoff
                for i in range(3):
                    try:
                        state[dt].update(
                            await self._load_data(
                                request["address"],
                                request["length"],
                                Operations.Get[dt],
                            )
                        )
                        break
                    except Exception as e:
                        print(f"Error: {e}, sleeping for {2 ** i} seconds")
                        # i = 4 here to ensure we sleep for a good amount of time if something went wrong or the HP is busy
                        if i < 4:
                            await asyncio.sleep(2**i)
                        else:
                            raise

        device_info: dict[str, any] = {}
        for name, definition in self._MAPPING.items():
            try:
                raw = state[definition["data_type"]][definition["address"]]
            except KeyError:
                continue
            match definition["type"]:
                case "int":
                    value = self.parse_ecoforest_int(raw)
                case "float":
                    value = self.parse_ecoforest_float(raw)
                case "boolean":
                    value = self.parse_ecoforest_bool(raw)
                case _:
                    continue
            if definition["entity_type"] == "temperature" and value == -999.9:
                value = None
            device_info[name] = value

        _LOGGER.debug(device_info)
        return EcoGeoDevice.build(self._model_name, device_info)

    async def _load_data(self, address, length, op_type) -> dict[int, str]:
        response = await self._request(
            data={"idOperacion": op_type, "dir": address, "num": length}
        )

        result = {}
        index = 0
        for i in range(address, address + length):
            result[i] = response[index]
            index += 1

        return result

    def _parse(self, response: str) -> list[str]:
        # Override default parse to get the proper data out
        lines = response.split("\n")

        a, b = lines[0].split("=")
        if (
            a
            not in [
                "error_geo_get_reg",
                "error_geo_get_bit",
                "error_geo_set_reg",
                "error_geo_set_bit",
            ]
            or b != "0"
        ):
            raise Exception("bad response: {}".format(response))

        return lines[1].split("&")[2:]

    def parse_ecoforest_int(self, value):
        result = int(value, 16)
        return result if result <= 32768 else result - 65536

    def parse_ecoforest_bool(self, value):
        return bool(int(value))

    def parse_ecoforest_float(self, value):
        return self.parse_ecoforest_int(value) / 10
