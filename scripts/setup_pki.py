#!/usr/bin/env python3
"""Create a local Root CA and OPC UA server/client certificates.

The generated layout is intentionally compatible with the paths already used by
the server and Node-RED:

  certs/ca/ca_key.pem
  certs/ca/ca_cert.pem
  certs/ca/ca_crl.pem
  certs/server/server_cert.der
  certs/server/server_key.pem
  certs/client/client_cert.der
  certs/client/client_key.pem
  certs/trusted_clients/ca_cert.der

Existing certificates are not overwritten unless ``--force`` is supplied.
Rotating the root CA invalidates all previously issued server/client certs.
"""
from __future__ import annotations

import argparse
import ipaddress
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "certs"
DEFAULT_SERVER_URI = "urn:freeopcua:python:server"
DEFAULT_CLIENT_URI = "urn:freeopcua:python:client"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(common_name: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "UA LinqInspect"),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])


def _write_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    path.chmod(0o600)


def _write_cert(
    path: Path,
    cert: x509.Certificate | x509.CertificateRevocationList,
    encoding: serialization.Encoding,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cert.public_bytes(encoding))
    path.chmod(0o644)


def _subject_key_id(public_key) -> x509.SubjectKeyIdentifier:
    return x509.SubjectKeyIdentifier.from_public_key(public_key)


def _authority_key_id(ca_cert: x509.Certificate) -> x509.AuthorityKeyIdentifier:
    return x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key())


def _make_ca(days: int) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = _new_key()
    subject = _name("UA LinqInspect Local Root CA")
    now = _utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(_subject_key_id(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _make_leaf(
    ca_key: rsa.RSAPrivateKey,
    ca_cert: x509.Certificate,
    common_name: str,
    days: int,
    san_entries: list[x509.GeneralName],
    extended_usage: x509.ObjectIdentifier,
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = _new_key()
    now = _utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=True,
                data_encipherment=True,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([extended_usage]), critical=False)
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(_subject_key_id(key.public_key()), critical=False)
        .add_extension(_authority_key_id(ca_cert), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def _make_crl(
    ca_key: rsa.RSAPrivateKey,
    ca_cert: x509.Certificate,
    days: int,
) -> x509.CertificateRevocationList:
    now = _utcnow()
    return (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca_cert.subject)
        .last_update(now - timedelta(minutes=5))
        .next_update(now + timedelta(days=days))
        .add_extension(x509.CRLNumber(x509.random_serial_number()), critical=False)
        .add_extension(_authority_key_id(ca_cert), critical=False)
        .sign(ca_key, hashes.SHA256())
    )


def _server_sans(hostname: str, server_uri: str) -> list[x509.GeneralName]:
    dns_names = []
    for name in ("localhost", hostname, socket.getfqdn()):
        if name and name not in dns_names:
            dns_names.append(name)
    entries: list[x509.GeneralName] = [x509.UniformResourceIdentifier(server_uri)]
    entries.extend(x509.DNSName(name) for name in dns_names)
    entries.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    return entries


def _print_cert(label: str, path: Path, cert: x509.Certificate) -> None:
    print(f"{label}: {path}")
    print(f"  subject: {cert.subject.rfc4514_string()}")
    print(f"  issuer:  {cert.issuer.rfc4514_string()}")
    print(f"  expires: {cert.not_valid_after_utc.isoformat()}")
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        print("  SAN:     (none)")
    else:
        print(f"  SAN:     {', '.join(str(value) for value in san)}")


def _ensure_writable(paths: list[Path], force: bool) -> None:
    existing = [path for path in paths if path.exists()]
    if existing and not force:
        print("Refusing to overwrite existing PKI files:", file=sys.stderr)
        for path in existing:
            print(f"  {path}", file=sys.stderr)
        print("Re-run with --force to rotate the local CA and reissue all certificates.", file=sys.stderr)
        raise SystemExit(2)


def setup_pki(out_dir: Path, force: bool, hostname: str, ca_days: int,
              server_days: int, client_days: int, crl_days: int,
              server_uri: str, client_uri: str) -> None:
    ca_dir = out_dir / "ca"
    server_dir = out_dir / "server"
    client_dir = out_dir / "client"
    trust_dir = out_dir / "trusted_clients"

    managed = [
        ca_dir / "ca_key.pem",
        ca_dir / "ca_cert.pem",
        ca_dir / "ca_cert.der",
        ca_dir / "ca_crl.pem",
        ca_dir / "ca_crl.der",
        server_dir / "server_cert.pem",
        server_dir / "server_cert.der",
        server_dir / "server_key.pem",
        server_dir / "ca_cert.pem",
        server_dir / "ca_cert.der",
        server_dir / "ca_crl.pem",
        server_dir / "ca_crl.der",
        client_dir / "client_cert.pem",
        client_dir / "client_cert.der",
        client_dir / "client_key.pem",
        client_dir / "ca_cert.pem",
        client_dir / "ca_cert.der",
        client_dir / "ca_crl.pem",
        client_dir / "ca_crl.der",
        trust_dir / "ca_cert.pem",
        trust_dir / "ca_cert.der",
    ]
    _ensure_writable(managed, force)

    ca_key, ca_cert = _make_ca(ca_days)
    ca_crl = _make_crl(ca_key, ca_cert, crl_days)
    server_key, server_cert = _make_leaf(
        ca_key,
        ca_cert,
        f"UA LinqInspect Server ({hostname})",
        server_days,
        _server_sans(hostname, server_uri),
        ExtendedKeyUsageOID.SERVER_AUTH,
    )
    client_key, client_cert = _make_leaf(
        ca_key,
        ca_cert,
        "UA LinqInspect Node-RED Client",
        client_days,
        [x509.UniformResourceIdentifier(client_uri)],
        ExtendedKeyUsageOID.CLIENT_AUTH,
    )

    _write_key(ca_dir / "ca_key.pem", ca_key)
    _write_cert(ca_dir / "ca_cert.pem", ca_cert, serialization.Encoding.PEM)
    _write_cert(ca_dir / "ca_cert.der", ca_cert, serialization.Encoding.DER)
    _write_cert(ca_dir / "ca_crl.pem", ca_crl, serialization.Encoding.PEM)
    _write_cert(ca_dir / "ca_crl.der", ca_crl, serialization.Encoding.DER)

    _write_key(server_dir / "server_key.pem", server_key)
    _write_cert(server_dir / "server_cert.pem", server_cert, serialization.Encoding.PEM)
    _write_cert(server_dir / "server_cert.der", server_cert, serialization.Encoding.DER)
    _write_cert(server_dir / "ca_cert.pem", ca_cert, serialization.Encoding.PEM)
    _write_cert(server_dir / "ca_cert.der", ca_cert, serialization.Encoding.DER)
    _write_cert(server_dir / "ca_crl.pem", ca_crl, serialization.Encoding.PEM)
    _write_cert(server_dir / "ca_crl.der", ca_crl, serialization.Encoding.DER)

    _write_key(client_dir / "client_key.pem", client_key)
    _write_cert(client_dir / "client_cert.pem", client_cert, serialization.Encoding.PEM)
    _write_cert(client_dir / "client_cert.der", client_cert, serialization.Encoding.DER)
    _write_cert(client_dir / "ca_cert.pem", ca_cert, serialization.Encoding.PEM)
    _write_cert(client_dir / "ca_cert.der", ca_cert, serialization.Encoding.DER)
    _write_cert(client_dir / "ca_crl.pem", ca_crl, serialization.Encoding.PEM)
    _write_cert(client_dir / "ca_crl.der", ca_crl, serialization.Encoding.DER)

    _write_cert(trust_dir / "ca_cert.pem", ca_cert, serialization.Encoding.PEM)
    _write_cert(trust_dir / "ca_cert.der", ca_cert, serialization.Encoding.DER)

    legacy_leaf = trust_dir / "client_cert.der"
    if legacy_leaf.exists():
        legacy_leaf.unlink()
        print(f"Removed legacy self-signed trusted leaf: {legacy_leaf}")

    print("\nGenerated local PKI:")
    _print_cert("Root CA", ca_dir / "ca_cert.pem", ca_cert)
    _print_cert("Server", server_dir / "server_cert.pem", server_cert)
    _print_cert("Client", client_dir / "client_cert.pem", client_cert)
    print(f"Root CA CRL: {ca_dir / 'ca_crl.pem'} (next update: {ca_crl.next_update_utc.isoformat()})")
    print("\nServer trust directory:", trust_dir)
    print("Server cert/key:       ", server_dir / "server_cert.der", server_dir / "server_key.pem")
    print("Client cert/key:       ", client_dir / "client_cert.der", client_dir / "client_key.pem")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a local CA-based OPC UA PKI")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output directory (default: {DEFAULT_OUT})")
    parser.add_argument("--force", action="store_true", help="rotate the CA and overwrite generated certificates")
    parser.add_argument("--hostname", default=socket.gethostname(), help="server hostname for the DNS SAN")
    parser.add_argument("--ca-days", type=int, default=3650, help="Root CA validity in days")
    parser.add_argument("--server-days", type=int, default=397, help="server certificate validity in days")
    parser.add_argument("--client-days", type=int, default=397, help="client certificate validity in days")
    parser.add_argument("--crl-days", type=int, default=397, help="Root CA CRL validity in days")
    parser.add_argument("--server-uri", default=DEFAULT_SERVER_URI, help="server application URI/ SAN URI")
    parser.add_argument("--client-uri", default=DEFAULT_CLIENT_URI, help="client application URI/ SAN URI")
    args = parser.parse_args()

    if min(args.ca_days, args.server_days, args.client_days, args.crl_days) <= 0:
        parser.error("certificate validity must be greater than zero days")

    setup_pki(
        args.out.expanduser().resolve(),
        args.force,
        args.hostname,
        args.ca_days,
        args.server_days,
        args.client_days,
        args.crl_days,
        args.server_uri,
        args.client_uri,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
