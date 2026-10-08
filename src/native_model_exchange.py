"""Explicit, SHA-bound native ChatGPT transport; no inference service fallback.

The scheduled task reads a private request, generates its own response and
returns a private response packet. This transport is not a claim that the native
model is pinned, that its output is true, or that canonical writes are authorized.
The candidate boundary must still validate source, evidence and model output.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

BACKEND = 'native_scheduled_chatgpt_unpinned'
MAX_PACKET_BYTES = 96 * 1024


class NativeExchangeError(ValueError):
    """Fixed reason only; never includes private prompt/response text."""


def encode(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def reject_constant(_: str) -> None:
    raise ValueError('non_json_constant')


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def make_request(prompt: str, schema: dict[str, Any], context: dict[str, str]) -> dict[str, Any]:
    body = {'schema_version': 1, 'type': 'NATIVE_MODEL_REQUEST',
            'inference_backend': BACKEND, 'prompt': prompt,
            'response_schema': schema, 'context': context}
    return {**body, 'request_sha256': hashlib.sha256(encode(body)).hexdigest()}


def make_response(request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Package an actual model response; does not generate or validate it."""
    return {'schema_version': 1, 'type': 'NATIVE_MODEL_RESPONSE',
            'inference_backend': BACKEND, 'request_sha256': request['request_sha256'],
            'response': response}


class NativeExchange:
    def __init__(self, *, request_path: Path | None = None,
                 response_path: Path | None = None) -> None:
        if (request_path is None) == (response_path is None):
            raise ValueError('exactly_one_native_exchange_path_required')
        path = request_path if request_path is not None else response_path
        if path is None or '00_LIBRARY' in {part.upper() for part in path.parts}:
            raise ValueError('native_exchange_path_invalid')
        self.request_path = request_path
        self.response_path = response_path

    def infer(self, prompt: str, schema: dict[str, Any],
              context: dict[str, str]) -> dict[str, Any]:
        request = make_request(prompt, schema, context)
        try:
            raw_request = encode(request)
        except (ValueError, TypeError):
            raise NativeExchangeError('native_request_invalid') from None
        if len(raw_request) > MAX_PACKET_BYTES:
            raise NativeExchangeError('native_request_budget')
        if self.request_path is not None:
            try:
                self.request_path.parent.mkdir(parents=True, exist_ok=True)
                with self.request_path.open('xb') as handle:
                    handle.write(raw_request)
            except FileExistsError:
                try:
                    with self.request_path.open('rb') as handle:
                        existing = handle.read(MAX_PACKET_BYTES + 1)
                    if existing != raw_request:
                        raise NativeExchangeError('native_request_conflict')
                except OSError:
                    raise NativeExchangeError('native_request_write_failed') from None
            except OSError:
                raise NativeExchangeError('native_request_write_failed') from None
            raise NativeExchangeError('native_request_ready')
        try:
            assert self.response_path is not None
            with self.response_path.open('rb') as handle:
                raw = handle.read(MAX_PACKET_BYTES + 1)
            if len(raw) > MAX_PACKET_BYTES:
                raise ValueError()
            response = json.loads(raw.decode('utf-8'), parse_constant=reject_constant,
                                  object_pairs_hook=unique_object)
        except (OSError, ValueError, UnicodeError):
            raise NativeExchangeError('native_response_invalid') from None
        if (not isinstance(response, dict)
                or set(response) != {'schema_version', 'type', 'inference_backend',
                                     'request_sha256', 'response'}
                or type(response['schema_version']) is not int or response['schema_version'] != 1
                or response['type'] != 'NATIVE_MODEL_RESPONSE'
                or response['inference_backend'] != BACKEND
                or not isinstance(response['response'], dict)):
            raise NativeExchangeError('native_response_invalid')
        if response['request_sha256'] != request['request_sha256']:
            raise NativeExchangeError('native_response_binding_mismatch')
        return response['response']
