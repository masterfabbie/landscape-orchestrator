"""Minimal SOAP 1.1 helpers for the mock services.

Only for the mock: the stdlib XML parser is not hardened against malicious input,
so the mock must not be exposed to untrusted networks.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
ET.register_namespace("SOAP-ENV", SOAP_ENV)


class SoapFault(Exception):
    def __init__(
        self,
        faultstring: str,
        faultcode: str = "SOAP-ENV:Server",
        http_status: int = 500,
    ) -> None:
        super().__init__(faultstring)
        self.faultstring = faultstring
        self.faultcode = faultcode
        self.http_status = http_status


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _to_value(element: ET.Element) -> Any:
    """Convert an element to a string (leaf) or dict; repeated child tags become lists."""
    if len(element) == 0:
        return (element.text or "").strip()
    result: dict[str, Any] = {}
    for child in element:
        key, value = _local(child.tag), _to_value(child)
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    return result


def parse_request(body: bytes) -> tuple[str, dict[str, Any]]:
    """Return (method name, parameters) of a SOAP request."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SoapFault("Invalid SOAP request", "SOAP-ENV:Client") from exc
    soap_body = root.find(f"{{{SOAP_ENV}}}Body")
    if soap_body is None or len(soap_body) == 0:
        raise SoapFault("SOAP body is empty", "SOAP-ENV:Client")
    call = soap_body[0]
    params = _to_value(call)
    return _local(call.tag), params if isinstance(params, dict) else {}


def items(value: Any) -> list[Any]:
    """Normalize a SOAP array (``<x><item>..</item><item>..</item></x>``) to a list."""
    if not isinstance(value, dict) or "item" not in value:
        return []
    item = value["item"]
    return item if isinstance(item, list) else [item]


def _append(parent: ET.Element, name: str, value: Any) -> None:
    element = ET.SubElement(parent, name)
    if isinstance(value, dict):
        for key, child in value.items():
            _append(element, key, child)
    elif isinstance(value, list):
        for child in value:
            _append(element, "item", child)
    elif isinstance(value, bool):
        element.text = "true" if value else "false"
    elif value is not None:
        element.text = str(value)


def _serialize(body_child: ET.Element) -> bytes:
    envelope = ET.Element(f"{{{SOAP_ENV}}}Envelope")
    ET.SubElement(envelope, f"{{{SOAP_ENV}}}Body").append(body_child)
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def build_response(prefix: str, namespace: str, method: str, payload: dict[str, Any]) -> bytes:
    ET.register_namespace(prefix, namespace)
    response = ET.Element(f"{{{namespace}}}{method}Response")
    for key, value in payload.items():
        _append(response, key, value)
    return _serialize(response)


def build_fault(fault: SoapFault) -> bytes:
    element = ET.Element(f"{{{SOAP_ENV}}}Fault")
    ET.SubElement(element, "faultcode").text = fault.faultcode
    ET.SubElement(element, "faultstring").text = fault.faultstring
    return _serialize(element)
