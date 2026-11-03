"""Lab-PKI: een eigen root-CA en servercertificaten voor de virtual servers.

Best practices die hier bewust zijn toegepast:
- de root-CA heeft Name Constraints: hij mag alleen certificaten voor het
  lab-domein (bijv. *.adclab.internal) uitgeven. Een desktop of laptop die deze CA
  vertrouwt, loopt dus geen risico voor andere sites, ook niet als de sleutel lekt;
- servercertificaten zijn maximaal 397 dagen geldig (browserlimiet 398), met
  SAN's (de CN telt niet meer), serverAuth als EKU en een eigen sleutel per certificaat;
- sleutels staan alleen in data/pki (0600, niet in git).
"""
from __future__ import annotations

import datetime as dt
import ipaddress

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .config import PKI, domain, gslb_domain

CA_CERT = PKI / "lab-root-ca.crt"
CA_KEY = PKI / "lab-root-ca.key"
CA_NAME = "FortiADC Lab Root CA"
LEAF_DAYS = 397


def _write_key(path, key):
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.TraditionalOpenSSL,
                                       serialization.NoEncryption()))
    path.chmod(0o600)


def _load_ca():
    key = serialization.load_pem_private_key(CA_KEY.read_bytes(), password=None)
    return x509.load_pem_x509_certificate(CA_CERT.read_bytes()), key


def ensure_ca(topo) -> x509.Certificate:
    """Maak de root-CA eenmalig aan; daarna blijft hij gelijk (anders moet de
    desktop opnieuw)."""
    if CA_CERT.exists() and CA_KEY.exists():
        return _load_ca()[0]
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FortiADC Lab"),
                      x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
    now = dt.datetime.now(dt.timezone.utc)
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.NameConstraints(permitted_subtrees=[x509.DNSName(domain(topo))],
                                                excluded_subtrees=None), critical=True)
            .add_extension(ski, critical=False)
            .sign(key, hashes.SHA256()))
    _write_key(CA_KEY, key)
    CA_CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert


def ca_pem() -> str:
    return CA_CERT.read_text()


def ca_fingerprint() -> str:
    cert = x509.load_pem_x509_certificate(CA_CERT.read_bytes())
    return cert.fingerprint(hashes.SHA256()).hex(":").upper()


def vs_names(topo, vs) -> list[str]:
    """SAN's voor een virtual server: de vriendelijke naam én de GSLB-naam."""
    names = []
    for h in vs["hosts"]:
        names.append(f"{h}.{domain(topo)}")
        if vs.get("gslb"):
            names.append(f"{h}.{gslb_domain(topo)}")
    return names


def cert_name(vs) -> str:
    return f"LAB-{vs['id'].upper()}"


def issue(topo, name: str, dns_names: list[str], ips: list[str] = ()) -> tuple[str, str]:
    """Servercertificaat (cert, key) als PEM; wordt hergebruikt zolang de SAN's
    gelijk zijn en het nog minstens 30 dagen geldig is."""
    crt_path, key_path = PKI / f"{name}.crt", PKI / f"{name}.key"
    if crt_path.exists() and key_path.exists():
        cur = x509.load_pem_x509_certificate(crt_path.read_bytes())
        san = cur.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        remaining = cur.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
        if sorted(san.get_values_for_type(x509.DNSName)) == sorted(dns_names) and remaining.days > 30:
            return crt_path.read_text(), key_path.read_text()
    ensure_ca(topo)
    ca_cert, ca_key = _load_ca()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.timezone.utc)
    sans = [x509.DNSName(n) for n in dns_names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FortiADC Lab"),
                                     x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0])]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    _write_key(key_path, key)
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return crt_path.read_text(), key_path.read_text()
