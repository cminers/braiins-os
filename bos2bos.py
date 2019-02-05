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
import tarfile
import shutil
import builder as bos_builder
import sys
import os
import io

import builder.hash as hash
import builder.nand as nand

from upgrade.backup import ssh_mode as get_mode
from upgrade.backup import MODE_SD, MODE_NAND
from builder.ssh import SSHManager, SSHError
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen
from pathlib import Path
from glob import glob

USERNAME = 'root'
PASSWORD = None

MINER_CFG_CONFIG = '/tmp/miner_cfg.config'

ZYNQ_PREFIX = 'zynq-'

BOS_PLATFORMS = {
    'zynq-am1-s9',
    'zynq-dm1-g9',
    'zynq-dm1-g19',
    'am1-s9',
    'dm1-g9',
    'dm1-g19'
}


class RestoreStop(Exception):
    pass


def mdt_write(ssh, local_path, mtd, name, erase=True, offset=0):
    # prepare mtd arguments
    mtd_args = ['mtd']
    if erase:
        mtd_args.extend(['-e', mtd])
    if offset > 0:
        mtd_args.extend(['-n', '-p', hex(offset)])
    mtd_args.extend(['write', '-', mtd])

    print("Writing {} to NAND partition '{}'...".format(name, mtd))
    with open(local_path, 'rb') as local, ssh.pipe(mtd_args) as remote:
        shutil.copyfileobj(local, remote.stdin)


def mtd_erase(ssh, mtd):
    print("Erasing NAND partition '{}'...".format(mtd))
    ssh.run(['mtd', 'erase', mtd])


def get_platform(ssh):
    stdout, _ = ssh.run('cat', '/tmp/sysinfo/board_name')
    return next(stdout).strip()


def get_env(ssh, name):
    stdout, _ = ssh.run('fw_printenv', '-n', name)
    return next(stdout).strip()


def get_ethaddr(ssh):
    stdout, _ = ssh.run('cat', '/sys/class/net/eth0/address')
    return next(stdout).strip()


def check_miner_cfg(ssh):
    _, stderr = ssh.run('fw_printenv', '-c', MINER_CFG_CONFIG)
    for _ in stderr:
        return False
    else:
        return True


def set_miner_cfg(ssh, config, rewrite_miner_cfg):
    miner_cfg_input = io.BytesIO()
    if not nand.write_miner_cfg_input(config, miner_cfg_input, use_default=rewrite_miner_cfg, ignore_empty=False):
        raise RestoreStop
    miner_cfg_input = miner_cfg_input.getvalue()

    if len(miner_cfg_input):
        if rewrite_miner_cfg:
            print("Setting default miner configuration...")
        else:
            print("Overriding miner configuration...")
        # write miner configuration to NAND
        with ssh.pipe('fw_setenv', '-c', MINER_CFG_CONFIG, '-s', '-') as remote:
            remote.stdin.write(miner_cfg_input)


def get_config(args, ssh, rewrite_miner_cfg):
    config = bos_builder.load_config(args.config)

    config.setdefault('miner', bos_builder.EmptyDict())
    config.setdefault('net', bos_builder.EmptyDict())

    if args.mac:
        config.net.mac = args.mac
    elif rewrite_miner_cfg and not config.net.get('mac'):
        config.net.mac = get_ethaddr(ssh)

    return config


def firmware_deploy(args, firmware_dir, stage2_dir, firmware_info):
    _, fw_platform = firmware_info

    # get file paths
    boot_bin = os.path.join(firmware_dir, 'boot.bin')
    uboot_img = os.path.join(firmware_dir, 'u-boot.img')
    fit_itb = os.path.join(stage2_dir, 'fit.itb')
    factory_bin_gz = os.path.join(stage2_dir, 'factory.bin.gz')
    system_bit_gz = os.path.join(stage2_dir, 'system.bit.gz')
    boot_bin_gz = os.path.join(stage2_dir, 'boot.bin.gz')
    miner_cfg_bin = os.path.join(stage2_dir, 'miner_cfg.bin')
    miner_cfg_config = os.path.join(stage2_dir, 'miner_cfg.config')

    print("Connecting to remote host...")
    with SSHManager(args.hostname, USERNAME, PASSWORD) as ssh:
        # detect mode
        platform = get_platform(ssh)
        mode = get_mode(ssh)
        print("Detected bOS platform: {}".format(platform))
        print("Detected bOS mode: {}".format(mode))

        if fw_platform != platform:
            print("Firmware image is incompatible with target platform!")
            if args.force:
                print("Forcing upgrade...")
            else:
                raise RestoreStop

        print("Uploading miner configuration file...")
        sftp = ssh.open_sftp()
        sftp.put(miner_cfg_config, MINER_CFG_CONFIG)
        sftp.close()

        rewrite_miner_cfg = args.rewrite_config or not check_miner_cfg(ssh)
        config = get_config(args, ssh, rewrite_miner_cfg)

        mdt_write(ssh, boot_bin, 'boot', 'SPL')
        mdt_write(ssh, uboot_img, 'uboot', 'U-Boot')
        mdt_write(ssh, fit_itb, 'recovery', 'recovery FIT image')
        mdt_write(ssh, factory_bin_gz, 'recovery', 'factory image', erase=False, offset=0x0800000)
        mdt_write(ssh, system_bit_gz, 'recovery', 'bitstream', erase=False, offset=0x1400000)
        # original firmware has different recovery partition without SPL bootloader
        if os.path.isfile(boot_bin_gz):
            mdt_write(ssh, boot_bin_gz, 'recovery', 'SPL bootloader', erase=False, offset=0x1500000)
        if rewrite_miner_cfg:
            mdt_write(ssh, miner_cfg_bin, 'miner_cfg', 'miner configuration')

        # set miner configuration
        set_miner_cfg(ssh, config, rewrite_miner_cfg)

        # erase rest of partitions
        mtds_for_erase = ['fpga1', 'fpga2', 'uboot_env']
        if mode == MODE_NAND:
            # active partition cannot be erased
            current_fw = int(get_env(ssh, 'firmware'))
            mtds_for_erase.append('firmware{}'.format((current_fw % 2) + 1))
        else:
            mtds_for_erase.extend(['firmware1', 'firmware2'])

        for mtd in mtds_for_erase:
            mtd_erase(ssh, mtd)

        ssh.run('sync')

        if mode == MODE_SD:
            print('Halting system...')
            print('Please turn off the miner and change jumper to boot it from NAND!')
            ssh.run('halt')
        else:
            print('Rebooting to restored firmware...')
            ssh.run('reboot')


def parse_fw_info(firmware_signature):
    exploded_signature = firmware_signature.split('_')
    if len(exploded_signature) < 3:
        return None
    fw_version = exploded_signature[-1]
    fw_platform = exploded_signature[1]
    if len(fw_version) != 21 or len(fw_version.split('-')) != 5:
        return None
    if fw_platform not in BOS_PLATFORMS:
        return None
    if fw_platform.startswith(ZYNQ_PREFIX):
        fw_platform = fw_platform[len(ZYNQ_PREFIX):]
    return fw_version, fw_platform


def main(args):
    url = args.firmware_url
    with TemporaryDirectory() as backup_dir:
        stream = open(url, 'rb') if os.path.isfile(url) else \
            urlopen(Request(args.firmware_url, headers={'User-Agent': 'Mozilla/5.0'}))
        stream = hash.HashStream(stream, 'md5')
        tar = tarfile.open(fileobj=stream, mode='r|*')
        print('Extracting firmware tarball...')
        tar.extractall(path=backup_dir)
        tar.close()
        stream.close()
        md5_digest = stream.hash.hexdigest()
        # find factory_transition with firmware directory
        firmware_dir = glob(os.path.join(backup_dir, '**', 'firmware'), recursive=True)
        if firmware_dir:
            firmware_dir = firmware_dir[0]
            firmware_signature = Path(os.path.relpath(firmware_dir, backup_dir)).parts[0]
            firmware_info = parse_fw_info(firmware_signature)
            stage2_path = os.path.join(firmware_dir, 'stage2.tgz')
        if not firmware_dir or not firmware_info or not os.path.isfile(stage2_path):
            print('Unsupported firmware tarball!')
            raise RestoreStop
        stage2_dir = os.path.join(firmware_dir, 'stage2')
        os.makedirs(stage2_dir)
        print('Extracting stage2 tarball...')
        tar = tarfile.open(stage2_path)
        tar.extractall(path=stage2_dir)
        tar.close()
        print("Detected bOS firmware image: {} ({})".format(*firmware_info))
        firmware_deploy(args, firmware_dir, stage2_dir, firmware_info)


if __name__ == "__main__":
    # execute only if run as a script
    parser = argparse.ArgumentParser()

    parser.add_argument('firmware_url',
                        help='URL to tarball with transitional bOS firmware from https://feeds.braiins-os.org/')
    parser.add_argument('hostname',
                        help='hostname of miner with bos firmware')
    parser.add_argument('--config',
                        help='path to configuration file')
    parser.add_argument('--rewrite-config', action='store_true',
                        help='force rewriting all miner settings with new/default configuration')
    parser.add_argument('--mac',
                        help='override MAC address')
    parser.add_argument('--force', action='store_true',
                        help='force installing incompatible firmware version')

    # parse command line arguments
    args = parser.parse_args(sys.argv[1:])

    try:
        main(args)
    except SSHError as e:
        print(str(e))
        sys.exit(1)
    except RestoreStop:
        sys.exit(2)
