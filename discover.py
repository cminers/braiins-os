#!/usr/bin/env python3

# Copyright (C) 2018  Braiins Systems s.r.o.
#
# This file is part of Braiins Build System (BB).
#
# BB is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import argparse
import itertools
import ipaddress
import asyncssh
import asyncio
import json
import sys

from asyncssh import create_connection
from asyncssh.misc import async_context_manager
from socket import socket, AF_INET, SOCK_DGRAM

ALL_HOSTS = '*'
IP_REPORT_PORT = 14235
IP_REPORT_BUFF = 1024


class NetworkInfo:
    PROTO_DHCP = 'dhcp'
    PROTO_STATIC = 'static'

    def __init__(self):
        self.mac = None
        self.ip = None
        self.proto = None
        self.hostname = None

    async def refresh(self, conn):
        self.mac = await self._get_mac(conn)
        self.ip = await self._get_ip(conn)
        return self

    async def _get_mac(self, conn):
        return await asyncssh_run(conn, "cat /sys/class/net/eth0/address")

    async def _get_ip(self, conn):
        return await asyncssh_run(conn, "/sbin/ip route get 1 | awk '{print $NF;exit}'")


class PoolInfo:
    def __init__(self, url, user, pwd):
        self.url = url
        self.user = user
        self.pwd = pwd


class DeviceInfo:
    INFO_UNKNOWN = 'unknown'

    def __init__(self):
        self.os = None
        self.net = None
        self.version = None
        self.mode = None
        self.ram_size = None
        self.pools = None
        self.note = None

    async def refresh(self, conn):
        self.os = await self._get_os(conn)
        self.net = await self._get_net_info_cls()().refresh(conn)
        self.version = await self._get_version(conn)
        self.mode = await self._get_mode(conn)
        self.ram_size = await self._get_ram_size(conn)
        self.pools = await self._get_pools(conn)
        self.note = await self._get_note(conn)
        return self

    def _get_net_info_cls(self):
        return NetworkInfo

    async def _get_os(self, conn):
        return DeviceInfo.INFO_UNKNOWN

    async def _get_version(self, conn):
        return DeviceInfo.INFO_UNKNOWN

    async def _get_mode(self, conn):
        return None

    @staticmethod
    def size2int(size, unit=None):
        scale = {
            'B': 1,
            'kB': 1024,
            'mB': 1024 * 1024
        }.get(unit or 'B')
        return int(size) * scale

    @staticmethod
    def int2size(size):
        for unit in ['B', 'KiB', 'MiB']:
            if size % 1024:
                break
            size //= 1024
        else:
            unit = 'GiB'
        return '{} {}'.format(size, unit)

    async def _get_ram_size(self, conn):
        ram_size = await asyncssh_run(conn, "grep MemTotal /proc/meminfo | awk '{print $2\" \"$3}'")
        return self.size2int(*ram_size.split())

    async def _get_cgminer_conf(self, conn):
        return await asyncssh_run(conn, "cat /etc/cgminer.conf")

    async def _get_pools(self, conn):
        cgminer_conf = await self._get_cgminer_conf(conn)
        if not cgminer_conf:
            return []
        cgminer_conf = json.loads(cgminer_conf)
        pools = []
        for pool in cgminer_conf['pools']:
            pools.append(PoolInfo(pool['url'], pool['user'], pool['pass']))
        return pools

    async def _get_note(self, conn):
        return None

    def get_short(self):
        info = list()
        info.append(self.os)
        info.append(self.version)
        self.mode and info.append('[{}]'.format(self.mode))
        self.ram_size and info.append('{{{} RAM}}'.format(self.int2size(self.ram_size)))
        if self.net.proto == NetworkInfo.PROTO_DHCP:
            info.append('{}({})'.format(self.net.proto, self.net.hostname))
        if self.pools:
            info.append('@' + self.pools[0].user)
        self.note and info.append('# {}'.format(self.note))
        return '{} ({}) | {}'.format(self.net.mac, self.net.ip, ' '.join(info))


class OpenWrtNetInfo(NetworkInfo):
    async def refresh(self, conn):
        await super().refresh(conn)
        config = await asyncssh_run(conn, 'uci show network.lan | sed "1d;s/network.lan.//;s/\'//g"')
        config = dict(line.split('=') for line in config.splitlines())
        self.proto = {
            'dhcp': self.PROTO_DHCP,
            'static': self.PROTO_STATIC
        }.get(config['proto'])
        if self.proto == self.PROTO_DHCP:
            self.hostname = config.get('hostname') or \
                            await asyncssh_run(conn, 'cat /proc/sys/kernel/hostname')
        return self


class AmNetInfo(NetworkInfo):
    async def refresh(self, conn):
        await super().refresh(conn)
        config = await asyncssh_run(conn, 'cat /config/network.conf')
        config = dict(line.split('=') for line in config.splitlines())
        if config['dhcp'] == 'true':
            self.proto = self.PROTO_DHCP
            self.hostname = config['hostname']
        else:
            self.proto = self.PROTO_STATIC
            self.hostname = None
        return self


class DmNetInfo(NetworkInfo):
    async def refresh(self, conn):
        await super().refresh(conn)
        config = await asyncssh_run(conn, 'cat /config/network/25-wired.network')
        config = dict(line.split('=') for line in config.splitlines() if not line.startswith('['))
        if config.get('DHCP') == 'yes':
            self.proto = self.PROTO_DHCP
            self.hostname = await asyncssh_run(conn, 'hostname')
        else:
            self.proto = self.PROTO_STATIC
            self.hostname = None
        return self


class BosInfo(DeviceInfo):
    def __init__(self, board_name):
        super().__init__()
        self.board_name = board_name

    @staticmethod
    async def create(conn):
        supported_names = [
            'dm1-g9',
            'dm1-g19',
            'dm1-g29',
            'am1-s9'
        ]
        board_name = await asyncssh_run(conn, "cat /tmp/sysinfo/board_name")
        return await BosInfo(board_name).refresh(conn) if board_name in supported_names else None

    def _get_net_info_cls(self):
        return OpenWrtNetInfo

    async def _get_os(self, conn):
        return 'bOS'

    async def _get_version(self, conn):
        version = await asyncssh_run(conn, "cat /etc/bos_version") or \
                  await asyncssh_run(conn, "opkg list-installed | sed -n '/firmware/s/.*- //p'") or \
                  DeviceInfo.INFO_UNKNOWN
        return '{}_{}'.format(self.board_name, version)

    @staticmethod
    async def _determine_mode(conn):
        if (await asyncssh_run(conn, "mount | grep -q '/dev/ubi0_2 on /overlay'", cat=False)).exit_status == 0:
            return 'nand'
        elif (await asyncssh_run(conn, "mount | grep -q '/dev/mmcblk0p2 on /overlay'", cat=False)).exit_status == 0:
            return 'sd'
        else:
            return 'recovery'

    async def _get_mode(self, conn):
        return await asyncssh_run(conn, "cat /etc/bos_mode") or \
               await self._determine_mode(conn) or \
               DeviceInfo.INFO_UNKNOWN

    async def _get_note(self, conn):
        return await asyncssh_run(conn, 'cat /etc/bos_note') or None


class AmInfo(DeviceInfo):
    def __init__(self, board_name):
        super().__init__()
        self.board_name = board_name
        self.fs_version = None
        self.miner_type = None
        self.logic_version = None

    @staticmethod
    async def create(conn):
        supported_names = [
            'XILINX',
            'C5'
        ]
        board_name = await asyncssh_run(conn, "cat /usr/bin/ctrl_bd")
        return await AmInfo(board_name).refresh(conn) if board_name in supported_names else None

    def _get_net_info_cls(self):
        return AmNetInfo

    async def refresh(self, conn):
        compile_time = await asyncssh_run(conn, "cat /usr/bin/compile_time")
        self.fs_version, self.miner_type, self.logic_version = compile_time.splitlines()[:3]
        return await super().refresh(conn)

    async def _get_os(self, conn):
        return 'Antminer'

    async def _get_version(self, conn):
        type = self.miner_type.split()[1]
        return '{} {} ({})'.format(type, self.fs_version, self.logic_version)

    async def _get_note(self, conn):
        return await asyncssh_run(conn, 'cat /config/note') or None

    async def _get_cgminer_conf(self, conn):
        return await asyncssh_run(conn, "cat /config/bmminer.conf")


async def detect_ssh(hostname):
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(hostname, 22),
                                           timeout=0.5)
    except OSError:
        return False
    except asyncio.futures.TimeoutError:
        return False

    writer.close()
    return True


class DmInfo(DeviceInfo):
    def __init__(self, board_name):
        super().__init__()
        self.board_name = board_name
        self.hw_revision = None

    @staticmethod
    async def create(conn):
        supported_names = [
            'G9',
            'G19',
            'G29'
        ]
        board_name = await asyncssh_run(conn, "cat /tmp/hwver")
        return await DmInfo(board_name).refresh(conn) if board_name in supported_names else None

    def _get_net_info_cls(self):
        return DmNetInfo

    async def refresh(self, conn):
        self.hw_revision = await asyncssh_run(conn, "cat /etc/hwrevision")
        return await super().refresh(conn)

    async def _get_os(self, conn):
        return 'DragonMint'

    async def _get_version(self, conn):
        return ' '.join(self.hw_revision.split()[1].split('.')).upper()

    async def _get_note(self, conn):
        return await asyncssh_run(conn, 'cat /config/note') or None


@async_context_manager
def asyncssh_connect(host, port, passwords):
    last_error = None
    for password in itertools.chain(passwords.get(host, []), passwords.get(ALL_HOSTS, [])):
        try:
            conn, _ = yield from create_connection(None, host, port, username='root', password=password,
                                                   known_hosts=None)
            break
        except asyncssh.misc.DisconnectError as e:
            if e.code == asyncssh.DISC_NO_MORE_AUTH_METHODS_AVAILABLE:
                last_error = e
                continue
    else:
        raise last_error

    return conn


async def asyncssh_run(conn, *args, cat=True):
    result = await asyncio.wait_for(conn.run(*args), timeout=0.5)
    return result.stdout.strip() if cat else result


async def detect_device(args, hostname):
    if not await detect_ssh(hostname):
        return

    try:
        async with asyncssh_connect(hostname, 22, args.passwords) as conn:
            for info_cls in [BosInfo, AmInfo, DmInfo]:
                device_info = await info_cls.create(conn)
                if device_info:
                    # print information about detected device
                    print(device_info.get_short())
                    break
    except asyncssh.misc.DisconnectError:
        return
    except asyncio.futures.TimeoutError:
        return
    except asyncssh.process.ProcessError:
        return


async def detect_devices(args, hostnames):
    for hostname in hostnames:
        await detect_device(args, str(hostname))


async def discover(args, hostnames):
    tasks = [detect_devices(args, hostnames) for _ in range(args.jobs)]
    await asyncio.wait(tasks)


def get_hostnames(hostname_list):
    hostnames_iters = []
    hostnames = []
    for hostname in hostname_list:
        try:
            ip_range = ipaddress.IPv4Network(hostname)
            if hostnames:
                hostnames_iters.append(tuple(hostnames))
                hostnames = []
            hostnames_iters.append(iter(ip_range))
        except ipaddress.AddressValueError:
            hostnames.append(hostname)
    if hostnames:
        hostnames_iters.append(tuple(hostnames))
    return itertools.chain(*hostnames_iters)


def get_passwords(passwords_path):
    passwords = {
        ALL_HOSTS: [None, '', 'admin', '123']
    }
    if passwords_path:
        with open(passwords_path, 'r') as passwords_file:
            for line in passwords_file:
                line = line.strip()
                host_pwd = line.split(':')
                host = ALL_HOSTS if len(host_pwd) == 1 else host_pwd[0]
                passwords.setdefault(host, []).append(host_pwd[-1])
    return passwords


class CommandManager:
    def __init__(self):
        self._argv = None
        self._args = None

    def set_args(self, argv, args):
        self._argv = argv
        self._args = args

    def scan(self):
        hostnames = get_hostnames(self._args.hostname)
        self._args.passwords = get_passwords(self._args.passwords)

        loop = asyncio.get_event_loop()
        loop.run_until_complete(discover(self._args, hostnames))
        loop.close()

    def listen(self):
        s = socket(AF_INET, SOCK_DGRAM)
        s.bind(('', IP_REPORT_PORT))
        while True:
            m = s.recvfrom(IP_REPORT_BUFF)
            ip_addr, mac_addr = m[0].decode('utf-8').split(',')
            print(self._args.format.format(IP=ip_addr, MAC=mac_addr))


def main(argv):
    command = CommandManager()

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    subparsers.required = True
    subparsers.dest = 'command'

    # create the parser for the "scan" command
    subparser = subparsers.add_parser('scan',
                                      help="actively scan provided range of address")
    subparser.set_defaults(func=command.scan)
    subparser.add_argument('hostname', nargs='+',
                           help='list of hostnames or subnet range')
    subparser.add_argument('--passwords',
                           help='path to file with list of possible passwords for connection')
    subparser.add_argument('-j', '--jobs', type=int, default=50,
                           help='number of concurrent jobs to scan network')

    # create the parser for the "listen" command
    subparser = subparsers.add_parser('listen',
                                      help="listen for incoming broadcast from devices")
    subparser.set_defaults(func=command.listen)
    subparser.add_argument('--format', action="store",
                           default="IP='{IP}', MAC='{MAC}'",
                           help="change default formatting string for device information; "
                                "the tags '{IP}' and '{MAC}' will be replaced with actual data")

    # parse command line arguments
    args = parser.parse_args(argv)
    # set arguments
    command.set_args(argv, args)
    # call sub-command
    args.func()


if __name__ == "__main__":
    # execute only if run as a script
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        print()
        pass
