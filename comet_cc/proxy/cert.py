"""Self-signed CA + leaf cert generation for the interception TLS endpoint.

Generated once on first `comet-cc install`; kept under $COMET_CC_HOME/certs.
Users add `ca.crt` to `NODE_EXTRA_CA_CERTS` so CC trusts our listener when
`ANTHROPIC_BASE_URL` is pointed at it.
"""
from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from comet_cc import config


def _write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.write_bytes(data)
    path.chmod(mode)


def _save_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    _write(path, pem, 0o600)


def _save_cert(path: Path, cert: x509.Certificate) -> None:
    _write(path, cert.public_bytes(serialization.Encoding.PEM))


def _gen_ca(cert_dir: Path) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "comet-cc-proxy-ca"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "CoMeT-CC"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0), critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    _save_key(cert_dir / "ca.key", key)
    _save_cert(cert_dir / "ca.crt", cert)
    return key, cert


def _gen_leaf(
    cert_dir: Path,
    ca_key: rsa.RSAPrivateKey,
    ca_cert: x509.Certificate,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "api.anthropic.com"),
    ])
    san = x509.SubjectAlternativeName([
        x509.DNSName("api.anthropic.com"),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(san, critical=False)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    _save_key(cert_dir / "leaf.key", key)
    _save_cert(cert_dir / "leaf.crt", cert)

    # Combined PEM for ssl.SSLContext.load_cert_chain convenience
    combined = (
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    _write(cert_dir / "server.pem", combined, 0o600)


def ensure_certs() -> dict[str, Path]:
    """Generate CA + leaf if missing. Idempotent."""
    d = config.cert_dir()
    ca_crt = d / "ca.crt"
    server = d / "server.pem"
    if ca_crt.exists() and server.exists():
        return {"ca": ca_crt, "server": server, "dir": d}

    ca_key_path = d / "ca.key"
    if ca_key_path.exists() and ca_crt.exists():
        ca_key = serialization.load_pem_private_key(
            ca_key_path.read_bytes(), password=None,
        )
        ca_cert = x509.load_pem_x509_certificate(ca_crt.read_bytes())
    else:
        ca_key, ca_cert = _gen_ca(d)
    _gen_leaf(d, ca_key, ca_cert)
    return {"ca": ca_crt, "server": server, "dir": d}
