from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import pathlib
import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PRODUCT_SLUG = "wav-linear-phase-eq-premium"
LICENSE_FILE_NAME = "license.json"
PUBLIC_KEY_ENV_VAR = "WAV_EQ_LICENSE_PUBLIC_KEY_PEM"
PUBLIC_KEY_FILE_ENV_VAR = "WAV_EQ_LICENSE_PUBLIC_KEY_FILE"


class LicenseError(ValueError):
    pass


@dataclass
class LicenseState:
    unlocked: bool
    status: str
    email: str = ""
    issued_at: str = ""
    install_id: str = ""
    product: str = ""
    code: str = ""


def _urlsafe_b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def get_runtime_base_dir() -> pathlib.Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return pathlib.Path(meipass).resolve()
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent


def get_license_storage_path(settings_path: pathlib.Path) -> pathlib.Path:
    return settings_path.with_name(LICENSE_FILE_NAME)


def get_installation_id() -> str:
    user_name = ""
    for env_name in ("USERNAME", "USER"):
        value = os.getenv(env_name, "").strip()
        if value:
            user_name = value
            break
    if not user_name:
        try:
            user_name = getpass.getuser().strip()
        except Exception:
            user_name = ""
    raw = "|".join(
        [
            platform.system(),
            platform.node(),
            user_name,
            str(pathlib.Path.home()),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()
    chunks = [digest[idx : idx + 6] for idx in range(0, 24, 6)]
    return "WAVEQ-" + "-".join(chunks)


def _load_public_key_bytes() -> bytes:
    env_pem = os.getenv(PUBLIC_KEY_ENV_VAR, "").strip()
    if env_pem:
        return env_pem.encode("utf-8")

    env_path = os.getenv(PUBLIC_KEY_FILE_ENV_VAR, "").strip()
    if env_path:
        path = pathlib.Path(env_path).expanduser()
        if path.exists():
            return path.read_bytes()

    default_path = get_runtime_base_dir() / "license_public_key.pem"
    if default_path.exists():
        return default_path.read_bytes()

    raise LicenseError(
        "No license public key is configured. Place license_public_key.pem beside the app or set WAV_EQ_LICENSE_PUBLIC_KEY_PEM."
    )


def _load_public_key() -> Ed25519PublicKey:
    try:
        return serialization.load_pem_public_key(_load_public_key_bytes())
    except Exception as exc:
        raise LicenseError(f"Could not load the public license key: {exc}") from exc


def _load_private_key_from_pem(private_key_pem: str | bytes) -> Ed25519PrivateKey:
    data = private_key_pem.encode("utf-8") if isinstance(private_key_pem, str) else private_key_pem
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except Exception as exc:
        raise LicenseError(f"Could not load the private license key: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise LicenseError("License key must be an Ed25519 private key.")
    return key


def generate_keypair() -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem


def generate_license_code(
    private_key_pem: str | bytes,
    install_id: str,
    email: str,
    product: str = PRODUCT_SLUG,
    issued_at: str | None = None,
) -> str:
    install_id = install_id.strip().upper()
    if not install_id:
        raise LicenseError("install_id is required.")
    private_key = _load_private_key_from_pem(private_key_pem)
    payload = {
        "version": 1,
        "product": product,
        "install_id": install_id,
        "email": email.strip(),
        "issued_at": issued_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = private_key.sign(payload_bytes)
    return f"{_urlsafe_b64encode(payload_bytes)}.{_urlsafe_b64encode(signature)}"


def decode_license_code(code: str) -> dict[str, Any]:
    code = code.strip()
    if "." not in code:
        raise LicenseError("Unlock code format is invalid.")
    payload_part, signature_part = code.split(".", 1)
    try:
        payload_bytes = _urlsafe_b64decode(payload_part)
        signature = _urlsafe_b64decode(signature_part)
    except Exception as exc:
        raise LicenseError(f"Unlock code could not be decoded: {exc}") from exc

    public_key = _load_public_key()
    try:
        public_key.verify(signature, payload_bytes)
    except InvalidSignature as exc:
        raise LicenseError("Unlock code signature is invalid.") from exc

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:
        raise LicenseError(f"Unlock code payload is invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise LicenseError("Unlock code payload is invalid.")
    if payload.get("product") != PRODUCT_SLUG:
        raise LicenseError("Unlock code is for a different product.")
    return payload


def validate_license_code(code: str, expected_install_id: str) -> LicenseState:
    payload = decode_license_code(code)
    install_id = str(payload.get("install_id", "")).strip().upper()
    if install_id != expected_install_id.strip().upper():
        raise LicenseError("This unlock code was issued for a different installation.")
    return LicenseState(
        unlocked=True,
        status=f"Premium unlocked for {payload.get('email', 'licensed user') or 'licensed user'}",
        email=str(payload.get("email", "")),
        issued_at=str(payload.get("issued_at", "")),
        install_id=install_id,
        product=str(payload.get("product", "")),
        code=code.strip(),
    )


def save_license_code(license_path: pathlib.Path, state: LicenseState) -> None:
    license_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "code": state.code,
        "email": state.email,
        "issued_at": state.issued_at,
        "install_id": state.install_id,
        "product": state.product,
    }
    license_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_saved_license(license_path: pathlib.Path, expected_install_id: str) -> LicenseState:
    if not license_path.exists():
        return LicenseState(unlocked=False, status="Free mode active. Premium FX are locked.")

    try:
        data = json.loads(license_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return LicenseState(unlocked=False, status=f"Saved license could not be loaded: {exc}")

    if not isinstance(data, dict) or not isinstance(data.get("code"), str):
        return LicenseState(unlocked=False, status="Saved license file is invalid.")

    try:
        return validate_license_code(data["code"], expected_install_id)
    except LicenseError as exc:
        return LicenseState(unlocked=False, status=f"Saved license is invalid: {exc}")


def activate_license_code(code: str, license_path: pathlib.Path, expected_install_id: str) -> LicenseState:
    state = validate_license_code(code, expected_install_id)
    save_license_code(license_path, state)
    return state


def _write_text(path: pathlib.Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Generate and validate WAV Linear-Phase EQ premium unlock codes.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    keygen = subparsers.add_parser("generate-keypair", help="Generate an Ed25519 keypair for license signing.")
    keygen.add_argument("--private-out", required=True)
    keygen.add_argument("--public-out", required=True)

    gen = subparsers.add_parser("generate-code", help="Generate an unlock code for one installation.")
    gen.add_argument("--private-key", required=True)
    gen.add_argument("--install-id", required=True)
    gen.add_argument("--email", required=True)

    verify = subparsers.add_parser("verify-code", help="Verify an unlock code against an installation id.")
    verify.add_argument("--code", required=True)
    verify.add_argument("--install-id", required=True)

    args = parser.parse_args()

    if args.command == "generate-keypair":
        private_pem, public_pem = generate_keypair()
        _write_text(pathlib.Path(args.private_out), private_pem)
        _write_text(pathlib.Path(args.public_out), public_pem)
        print(f"Wrote private key to {args.private_out}")
        print(f"Wrote public key to {args.public_out}")
        return 0

    if args.command == "generate-code":
        private_pem = pathlib.Path(args.private_key).read_text(encoding="utf-8")
        print(generate_license_code(private_pem, args.install_id, args.email))
        return 0

    state = validate_license_code(args.code, args.install_id)
    print(json.dumps(state.__dict__, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
