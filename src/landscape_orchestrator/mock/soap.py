"""SOAP request parsing and response building for the mock services."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from ..soapxml import (
    SOAP_ENV,
    XmlError,
    build_element,
    envelope,
    items,
    local,
    parse_envelope,
    to_value,
)

__all__ = ["SoapFault", "build_fault", "build_response", "items", "parse_request"]


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


def parse_request(body: bytes) -> tuple[str, dict[str, Any]]:
    """Return (method name, parameters) of a SOAP request."""
    try:
        call = parse_envelope(body)
    except XmlError as exc:
        raise SoapFault("Invalid SOAP request", "SOAP-ENV:Client") from exc
    params = to_value(call)
    return local(call.tag), params if isinstance(params, dict) else {}


def build_response(prefix: str, namespace: str, method: str, payload: dict[str, Any]) -> bytes:
    ET.register_namespace(prefix, namespace)
    return envelope(build_element(namespace, f"{method}Response", payload))


def build_fault(fault: SoapFault) -> bytes:
    element = ET.Element(f"{{{SOAP_ENV}}}Fault")
    ET.SubElement(element, "faultcode").text = fault.faultcode
    ET.SubElement(element, "faultstring").text = fault.faultstring
    return envelope(element)
