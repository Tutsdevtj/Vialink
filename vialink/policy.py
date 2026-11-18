import ipaddress
import socket
import sys
from urllib.parse import urlsplit

MIB = 1024 * 1024
MAX_FILE = 300_000_000
MAX_ANALYSIS_DISK = 16 * MIB
MAX_JOB_DISK = 2 * MAX_FILE + 8 * MIB
MAX_URL_LENGTH = 4096


class UserError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


class PrivateAddressError(OSError):
    pass


def public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.version == 6:
        if address.scope_id is not None or address.sixtofour or address.teredo:
            return False
        if address.ipv4_mapped:
            address = address.ipv4_mapped
    return address.is_global and not address.is_multicast and not address.is_reserved


def check_url(url):
    invalid = 'Informe uma URL completa que comece com http:// ou https://.'
    if not url or len(url) > MAX_URL_LENGTH or '\\' in url or '#__youtubedl_smuggle' in url:
        raise UserError(invalid)
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise UserError(invalid)
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if parts.scheme not in {'http', 'https'} or not host or parts.username is not None:
            raise UserError(invalid)
        if parts.port == 0 or '%' in host:
            raise UserError(invalid)
        host = host.rstrip('.').encode('idna').decode('ascii').lower()
    except (ValueError, UnicodeError):
        raise UserError(invalid) from None

    try:
        ipaddress.ip_address(host)
    except ValueError:
        if '.' in host and not host.endswith(('.localhost', '.local', '.internal', '.home.arpa')):
            return url
    else:
        if public_ip(host):
            return url
    raise UserError('Use um link público. Endereços locais ou privados são bloqueados.')


def restrict_worker_network():
    """Check all DNS answers and the numeric endpoint of each Python TCP connection."""
    resolve = socket.getaddrinfo

    def public_answers(*args, **kwargs):
        answers = resolve(*args, **kwargs)
        if any(item[0] not in (socket.AF_INET, socket.AF_INET6) or not public_ip(item[4][0])
               for item in answers):
            raise PrivateAddressError('A resolução DNS retornou um endereço bloqueado.')
        return answers

    def check_connection(event, args):
        if event != 'socket.connect':
            return
        connection, address = args
        if (connection.family not in (socket.AF_INET, socket.AF_INET6)
                or not public_ip(address[0]) or (len(address) == 4 and address[3])):
            raise PrivateAddressError('Tentativa de conexão com um endereço bloqueado.')

    socket.getaddrinfo = public_answers
    sys.addaudithook(check_connection)
