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

import logging
import subprocess
import shutil
import tarfile
import copy
import gzip
import git
import io
import os
import sys
import glob
import filecmp
import tempfile

import miner.nand as nand

from itertools import chain
from collections import OrderedDict, namedtuple
from contextlib import contextmanager
from termcolor import colored
from functools import partial
from datetime import datetime, timezone
from doit.tools import run_once, config_changed, check_timestamp_unchanged
from urllib.request import Request, urlopen

from miner.config import ListWalker, RemoteWalker, load_config
from miner.repo import RepoProgressPrinter
from miner.ssh import SSHManager
from miner.packages import Packages


class BuilderStop(Exception):
    """
    Exception raised when builder detected error and stopped immediately.
    """
    pass


ImageSd = namedtuple('ImageSd', ['boot', 'uboot', 'fpga', 'kernel'])
ImageRecovery = namedtuple('ImageRecovery', ['boot', 'uboot', 'fpga', 'kernel', 'factory'])
ImageNand = namedtuple('ImageNand', ['boot', 'uboot', 'fpga', 'factory', 'sysupgrade'])
ImageUpgrade = namedtuple('ImageUpgrade', ['boot', 'uboot', 'fpga', 'kernel', 'kernel_recovery', 'factory'])
ImageFeeds = namedtuple('ImageFeeds', ['key', 'packages', 'sysupgrade'])


def get_stream_size(stream):
    stream_pos = stream.tell()
    stream_size = stream.seek(0, os.SEEK_END)
    stream.seek(stream_pos)
    return stream_size


class Builder:
    """
    Main class for building the Miner firmware based on the LEDE (OpenWRT) project.

    It prepares the LEDE source code and all related projects.
    Then it is possible to configure the project and build the firmware.
    The class also provides miscellaneous methods for cleaning build directories, firmware deployment and debugging
    on target platform.
    """
    DEFAULT_CONFIG = os.path.join('configs', 'default.yml')
    WHATS_NEW = 'whatsnew.md'
    WHATS_NEW_COMMENT = "Create version header for '{}'".format(WHATS_NEW)

    LEDE_META_DIR = 'miner'
    LEDE_META_SSH = 'ssh.py'
    LEDE_META_HWID = 'hwid.py'

    LEDE = 'lede'
    LUCI = 'luci'
    PLATFORM = 'platform'
    UBOOT = 'u-boot'
    LINUX = 'linux'
    CGMINER = 'cgminer'
    FEEDS_CONF = 'feeds.conf'
    FEEDS_DIR = 'feeds'
    CONFIG_NAME = '.config'
    BUILD_KEY_NAME = 'key-build'
    BUILD_KEY_PUB_NAME = 'key-build.pub'

    UENV_TXT = 'uEnv.txt'

    MTD_BITSTREAM = 'fpga'

    UPGRADE_IMAGE_PREFIX = 'braiins-os'

    ARCHIVE_FLAG_FLAT = 'flat'

    ARCHIVE_TGZ = 'tar.gz'
    ARCHIVE_TBZ2 = 'tar.bz2'

    UPGRADE_DM1 = 'dm1'
    UPGRADE_DM1_SSH = (UPGRADE_DM1, 'ssh')
    UPGRADE_DM1_TELNET = (UPGRADE_DM1, 'telnet')
    UPGRADE_AM1 = 'am1'
    UPGRADE_AM1_SSH = (UPGRADE_AM1, 'ssh')
    UPGRADE_AM1_WEB = (UPGRADE_AM1, 'web')
    UPGRADE_VERSION = {
        'zynq-dm1': (
            (UPGRADE_DM1_SSH, (ARCHIVE_TBZ2, [])),
            (UPGRADE_DM1_TELNET, (ARCHIVE_TBZ2, []))
        ),
        'zynq-am1': (
            (UPGRADE_AM1_SSH, (ARCHIVE_TBZ2, [])),
            (UPGRADE_AM1_WEB, (ARCHIVE_TGZ, [ARCHIVE_FLAG_FLAT]))
        )
    }

    UPGRADE_DIR = 'upgrade'
    UPGRADE_FIRMWARE_DIR = 'firmware'
    UPGRADE_UBOOT_ENV = 'uboot_env.bin'
    UPGRADE_UBOOT_ENV_CONFIG = 'uboot_env.config'
    UPGRADE_UBOOT_ENV_TXT = 'uboot_env.txt'
    UPGRADE_MINER_CFG = 'miner_cfg.bin'
    UPGRADE_MINER_CFG_CONFIG = 'miner_cfg.config'
    UPGRADE_SCRIPT_SRC = 'upgrade.py'
    UPGRADE_SCRIPT = 'upgrade2bos.py'
    UPGRADE_PLATFORM_SCRIPT_SRC = 'platform.py'
    UPGRADE_PLATFORM_SCRIPT = 'platform.py'
    UPGRADE_BACKUP_SCRIPT_SRC = 'backup.py'
    UPGRADE_BACKUP_SCRIPT = 'backup.py'
    UPGRADE_RESTORE_SCRIPT_SRC = 'restore.py'
    UPGRADE_RESTORE_SCRIPT = 'restore2factory.py'
    UPGRADE_TRANSFER_SCRIPT_SRC = 'transfer.py'
    UPGRADE_TRANSFER_SCRIPT = 'transfer.py'
    UPGRADE_INIT_SCRIPT_SRC = '__init__.py'
    UPGRADE_INIT_SCRIPT = '__init__.py'
    UPGRADE_SCRIPT_REQUIREMENTS_SRC = 'requirements.txt'
    UPGRADE_SCRIPT_REQUIREMENTS = 'requirements.txt'
    UPGRADE_STAGE1_CONTROL_SRC = 'CONTROL'
    UPGRADE_STAGE1_CONTROL = 'CONTROL'
    UPGRADE_STAGE1_SCRIPT = 'stage1.sh'
    UPGRADE_STAGE2_SCRIPT = 'stage2.sh'
    UPGRADE_STAGE2 = 'stage2.tgz'
    UPGRADE_FACTORY_RESTORE_SRC = 'restore.sh'
    UPGRADE_FACTORY_RESTORE = 'restore.sh'
    UPGRADE_AM_RUNME_SRC = 'runme.sh'
    UPGRADE_AM_UBI_INFO_SRC = 'ubi_info'
    UPGRADE_AM_RUNME = 'runme.sh'
    UPGRADE_AM_UBI_INFO = 'ubi_info'

    # sysupgrade attributes
    SYSUPGRADE_ATTR_MAJOR = 'major'
    SYSUPGRADE_ATTR_REQUIRE = 'require'
    SYSUPGRADE_ATTR_INCLUDE = 'include'

    # feeds index constants
    FEEDS_INDEX = 'Packages'
    FEEDS_ATTR_PACKAGE = 'Package'
    FEEDS_ATTR_FILENAME = 'Filename'
    FEEDS_ATTR_VERSION = 'Version'
    FEEDS_ATTR_REQUIRE = 'Require'
    FEEDS_EXCLUDED_ATTRIBUTES = ['Source', 'Maintainer']

    FEED_FIRMWARE = 'firmware'

    # list of supported utilities
    LEDE_MKENVIMAGE = 'mkenvimage'
    LEDE_USIGN = 'usign'

    LEDE_UTILITIES = {
        LEDE_MKENVIMAGE: os.path.join('build_dir', 'host', 'u-boot-2014.10', 'tools', 'mkenvimage'),
        LEDE_USIGN: os.path.join('staging_dir', 'host', 'bin', 'usign')
    }

    # configuration file constants
    CONFIG_DEVICES = ['nand', 'recovery', 'sd', 'upgrade']
    PACKAGE_LIST_PREFIX = 'image_'

    def _split_platform(self, platform: str=None):
        """
        Return target and sub-target for selected platform

        :param platform:
            Name of selected platform.
            When platform is omitted then platform from current configuration is used.
        :return:
            Pair of two strings with platform target and sub-target.
        """
        platform = platform or self._config.miner.platform
        return tuple(platform.split('-', 1))

    def _get_sysupgrade_attr(self, name):
        """
        Get sysupgrade attribute for current platform specified by matching pattern in
        the configuration.

        :param name:
            Name of attribute.
        :return:
            Configuration for sysupgrade attribute for current platform.
        """
        sysupgrade = self._config.build.sysupgrade

        # find attributes for current platform with prefix pattern
        for pattern, value in sorted(sysupgrade.items(), reverse=True):
            if self._config.miner.platform.startswith(pattern) and value.get(name):
                return value.get(name)

    def _write_target_config(self, stream, config):
        """
        Write all settings concerning target configuration

        :param stream:
            Opened stream for writing configuration.
        :param config:
            Configuration name prefix.
        """
        image_packages = load_config(self._config.build.packages)

        platform = self._config.miner.platform
        target_name, _ = self._split_platform(platform)
        device_name = platform.replace('-', '_')
        bitstream_path = self._get_bitstream_path()

        stream.write('{}{}=y\n'.format(config, target_name))
        stream.write('{}{}=y\n'.format(config, device_name))
        stream.write('{}MULTI_PROFILE=y\n'.format(config))
        stream.write('{}PER_DEVICE_ROOTFS=y\n'.format(config))

        for image in self.CONFIG_DEVICES:
            packages = ' '.join(ListWalker(image_packages, self.PACKAGE_LIST_PREFIX + image))
            stream.write('{}DEVICE_{}_DEVICE_{}=y\n'.format(config, device_name, image))
            stream.write('{}DEVICE_PACKAGES_{}_DEVICE_{}="{}"\n'.format(config, device_name, image, packages))

        logging.debug("Set bitstream target path to '{}'".format(bitstream_path))
        stream.write('{}FPGA="{}"\n'.format(config, bitstream_path))

    def _write_sysupgrade(self, stream, config):
        """
        Write all settings concerning sysupgrade components

        :param stream:
            Opened stream for writing configuration.
        :param config:
            Configuration name prefix.
        """
        components = [
            ('command', 'COMMAND'),
            ('spl', 'SPL'),
            ('uboot', 'UBOOT'),
            ('fpga', 'FPGA')
        ]

        # get includes from platform sysupgrade attribute
        includes = self._get_sysupgrade_attr(self.SYSUPGRADE_ATTR_INCLUDE)

        for src_name, dst_name in components:
            if src_name in includes:
                stream.write('{}{}=y\n'.format(config, dst_name))

    def _write_firmware_major(self, stream, config):
        """
        Write major firmware version.

        :param stream:
            Opened stream for writing configuration.
        :param config:
            Configuration name prefix.
        :return:
            Current firmware version.
        """
        fw_major = self._get_sysupgrade_attr(self.SYSUPGRADE_ATTR_MAJOR) == 'yes'
        fw_major = self.get_firmware_version() if fw_major else self._get_sysupgrade_attr(self.SYSUPGRADE_ATTR_REQUIRE)
        logging.debug("Set firmware major version to '{}'".format(fw_major))
        stream.write('{}="{}"\n'.format(config, fw_major))

    def _write_firmware_version(self, stream, config):
        """
        Write current firmware version.

        :param stream:
            Opened stream for writing configuration.
        :param config:
            Configuration name prefix.
        :return:
            Current firmware version.
        """
        fw_version = self.get_firmware_version()
        logging.debug("Set firmware version to '{}'".format(fw_version))
        stream.write('{}="{}"\n'.format(config, fw_version))

    def _write_firmware_require(self, stream, config):
        """
        Write previous firmware version required by this firmware

        :param stream:
            Opened stream for writing configuration.
        :param config:
            Configuration name prefix.
        :return:
            Previous firmware version required by this firmware.
        """
        fw_require = self._get_sysupgrade_attr(self.SYSUPGRADE_ATTR_REQUIRE)
        logging.debug("Set required firmware version to '{}'".format(fw_require))
        stream.write('{}="{}"\n'.format(config, fw_require))

    def _write_external_path(self, stream, config, repo_name: str, name: str):
        """
        Write absolute path to external directory of corespondent repository

        :param stream:
            Opened stream for writing configuration.
        :param config:
            Configuration name prefix.
        :param repo_name:
            Name of repository.
        :param name:
            Descriptive name of repository.
        :return:
            Absolute path to external directory.
        """
        external_dir = self._get_repo_path(repo_name)
        logging.debug("Set external {} tree to '{}'".format(name, external_dir))
        stream.write('{}="{}"\n'.format(config, external_dir))

    GENERATED_CONFIGS = [
        ('CONFIG_TARGET_', _write_target_config),
        ('CONFIG_SYSUPGRADE_WITH_', _write_sysupgrade),
        ('CONFIG_FIRMWARE_MAJOR', _write_firmware_major),
        ('CONFIG_FIRMWARE_VERSION', _write_firmware_version),
        ('CONFIG_FIRMWARE_REQUIRE', _write_firmware_require),
        ('CONFIG_EXTERNAL_KERNEL_TREE', partial(_write_external_path, repo_name=LINUX, name='kernel')),
        ('CONFIG_EXTERNAL_CGMINER_TREE', partial(_write_external_path, repo_name=CGMINER, name='CGMiner')),
        ('CONFIG_EXTERNAL_UBOOT_TREE', partial(_write_external_path, repo_name=UBOOT, name='U-Boot')),
        # remove all commented CONFIG_TARGET_
        ('# CONFIG_TARGET_', None)
    ]

    def __init__(self, config, argv):
        """
        Initialize builder for specific configuration

        :param config:
            Configuration object which has its attributes stored in dictionary or list.
            The key of dictionary can be also accessed as an object attribute.
        :param argv:
            Command line arguments for better help printing.
        """
        class StrFormatter:
            """
            Formatter class for expanding configuration string attributes

            The string attribute can contain standard format tags '{NAME}' with the NAME from following list:
            * platform - the whole platform name in format <target>-<subtarget>
            * target - the name of target platform e.g. zynq
            * subtarget - the name of device e.g. dm1-g9, dm1-g19
            """
            def __init__(self, builder: Builder):
                """
                Initialize formatter object

                :param builder:
                    The builder object for expanding tags for current configuration.
                """
                repo_meta = git.Repo()
                repo_url = repo_meta.remotes.origin.url

                platform = config.miner.platform
                split_platform = builder._split_platform(platform)
                self._format_tags = {
                    'working_dir': repo_meta.working_dir,
                    'meta_repo': repo_url.rsplit('/', 1)[0],
                    'platform': platform,
                    'target': split_platform[0],
                    'subtarget': split_platform[1],
                    'subtarget_family': split_platform[1].split('-')[0]
                }
                # meta_branch cannot be used when repository is detached
                if not repo_meta.head.is_detached:
                    repo_branch = repo_meta.active_branch.name
                    self._format_tags['meta_branch'] = repo_branch

            def add_tag(self, name, value):
                """
                Add new format tag

                :param name:
                    Name of tag.
                :param value:
                    Value which will be used for tag replacement.
                """
                self._format_tags[name] = value

            def __call__(self, value: str) -> str:
                """
                Create callable object used in configuration parset for tag expansion

                :param value:
                    Format string with tags specified in format {NAME}.
                :return:
                    String with expanded tags.
                """
                return value.format(**self._format_tags)

        self._config = copy.deepcopy(config)
        self._config.formatter = StrFormatter(self)
        self._argv = argv
        self._build_dir = os.path.join(os.path.abspath(self._config.build.dir), self._config.build.name)
        # add build_dir tag after it has been initialized
        self._config.formatter.add_tag('build_dir', self._build_dir)
        # set working directory to LEDE root directory
        self._working_dir = self._get_repo_path(self.LEDE)
        self._tmp_dir = os.path.join(self._working_dir, 'tmp')
        self._repos = OrderedDict()
        self._init_repos()

    @property
    def build_dir(self):
        """
        Return build directory for current configuration
        """
        return self._build_dir

    @property
    def configuration(self):
        """
        Return current configuration
        """
        return self._config

    def _run(self, *args, path=None, input=None, output=False, init=None):
        """
        Run system command in LEDE source directory

        The running environment is checked and when system command returns error it throws an exception.
        Two key arguments are supported. The `path` is for altering PATH environment variable and the `output`
        specifies if stdout is captured and returned by this method.

        :param args:
            First item is a command executed in the LEDE source directory.
            Remaining items are passed into the program as arguments.
            If args[0] is a list then this list is used instead of args.

            This allows use method in two forms:

            - `self._run([cmd, arg1, arg2])`
            - `self._run(cmd, arg1, arg2)`.
        :param path:
            List of directories prepended to PATH environment variable.
        :param input:
            A string which is passed to the subprocess's stdin.
        :param output:
            If true then method returns captured stdout otherwise stdout is printed to standard output.
        :param init:
            An object to be called in the child process just before the child is executed.
        :return:
            Captured stdout when `output` argument is set to True.
        """
        env = None
        cwd = self._working_dir
        stdout = subprocess.PIPE if output else None

        if path:
            env = os.environ.copy()
            env['PATH'] = ':'.join((*path, env['PATH']))
        if type(args[0]) is list:
            args = args[0]
        if path:
            logging.debug("Set PATH environment variable to '{}'".format(env['PATH']))

        logging.debug("Run '{}' in '{}'".format(' '.join(args), cwd))

        process = subprocess.run(args, input=input, stdout=stdout, check=True, cwd=cwd, env=env, preexec_fn=init)
        if output:
            return process.stdout

    def get_firmware_version(self, short=False, local_time=False) -> str:
        """
        Return version name for firmware

        The firmware version is in a form 'firmware_<date>-<patch_level>-<lede_commit>(-dirty)'
        The patch level is incremented when several firmwares have beenRemoteWalker released in the same day.
        The current firmware version is get from git tag which is created when release is done.

        :param short:
            Return unique short version without commit suffix.
        :param local_time:
            Use local time for firmware version instead of committed date of head.
        :return:
            String with firmware version without 'firmware_' prefix.
        """
        repo = git.Repo()

        # get commit time in RFC 3339 format
        if local_time:
            commit_time = datetime.utcnow()
        else:
            commit_timestamp = repo.head.object.committed_date
            commit_time = datetime.fromtimestamp(commit_timestamp, timezone.utc)

        fw_current = '{}_{:%Y-%m-%d}-'.format(self.FEED_FIRMWARE, commit_time)

        # filter out only versions for current date
        fw_tags = (str(tag) for tag in repo.tags if str(tag).startswith(fw_current))
        # get latest version
        fw_latest = next(iter(sorted(fw_tags, reverse=True)), None)

        commit = repo.head.object.hexsha[:8]
        dirty = '-dirty' if repo.is_dirty() else ''

        if fw_latest:
            fw_patch_level, fw_commit = fw_latest[len(fw_current):].split('-', 2)[:2]
            patch_level = int(fw_patch_level)
            if fw_commit != commit:
                # create new version
                patch_level += 1
        else:
            # when any release hasn't been created then use initial patch level 0
            patch_level = 0

        prefix = '{:%Y-%m-%d}-{}'.format(commit_time, patch_level)
        return '{}{}'.format(prefix, dirty) if short else '{}-{}{}'.format(prefix, commit, dirty)

    def _get_repo(self, name: str) -> git.Repo:
        """
        Return git repository by its name

        :param name: The name of repository as it has been specified in configuration file.
        :return: Associated git repository or raise exception if the repository does not exist.
        """
        return self._repos[name]

    def _get_repo_path(self, name: str) -> str:
        """
        Return absolute path to repository specified by its name

        :param name: The name of repository as it has been specified in configuration file.
        :return: Absolute path to the repository.
        """
        return os.path.join(self._build_dir, name)

    def _get_config_paths(self):
        """
        Return absolute paths to default and current configuration file

        - `default` configuration file points to a file specified in `build.config`
        - `current` configuration file points to a file in LEDE build directory

        :return:
            Pair of absolute paths to default and current configuration file.
        """
        config_src_path = os.path.abspath(self._config.build.config)
        config_dst_path = os.path.join(self._working_dir, self.CONFIG_NAME)
        return config_src_path, config_dst_path

    def _use_glibc(self):
        """
        Check if glibc is used for build

        :return:
            True when configuration file is set for use of glibc.
        """
        config_path, _ = self._get_config_paths()
        with open(config_path, 'r') as config:
            return any((line.startswith('CONFIG_LIBC="glibc"') for line in config))

    def _get_hostname(self) -> str:
        """
        Return hostname derived from miner MAC address

        :return:
            Miner hostname for current configuration.
        """
        mac = self._config.net.mac
        return 'miner-' + ''.join(mac.split(':')[-3:]).lower()

    def _get_utility(self, name: str):
        """
        Return LEDE utility when it exists or raise an exception

        :param name:
            Name of LEDE utility.
        :return:
            Path to specified LEDE utility.
        """
        utility_path = os.path.join(self._working_dir, self.LEDE_UTILITIES[name])
        if not os.path.exists(utility_path):
            logging.error("Missing utility '{}'".format(utility_path))
            raise BuilderStop
        return utility_path

    def _init_repos(self):
        """
        Initialize all repositories specified in configuration file

        The list of repositories is stored under `remote.repos`.

        If repository is not cloned yet then None is used otherwise the repository is opened by `git.Repo`.
        """
        error = False
        for name in self._config.remote.repos:
            path = self._get_repo_path(name)
            logging.debug("Init repo '{}' in '{}'".format(name, path))
            repo = None
            try:
                repo = git.Repo(path)
            except git.exc.NoSuchPathError:
                logging.debug("Missing directory '{}'".format(path))
            except git.exc.InvalidGitRepositoryError:
                if os.listdir(path):
                    logging.error("Invalid Git repository '{}'".format(path))
                    error = True
                else:
                    logging.warning("Empty Git repository '{}'".format(path))
            self._repos[name] = repo
        if error:
            raise BuilderStop

    def _clone_repo_doit(self, remote):
        """
        Clone repository when it is missing or remote server is changed

        :param remote:
            Named tuple with information about remote repository.
        :return:
            Generator returning dictionary with dependencies and action for doit task.
        """
        name = remote.name
        path = self._get_repo_path(name)
        repo = self._repos[name]

        yield {
            'name': name,
            'uptodate': [config_changed(repo.remotes.origin.url if repo else ''),
                         config_changed(remote.uri)]
        }

        repo = self._repos[name]
        if repo and (repo.is_dirty(untracked_files=True) or
                     (not repo.head.is_detached and self._count_commits(repo)[0] != 0)):
            # old repo exists and should not be removed when there are changes
            logging.error("URI of repository '{}' has changed but new repository cannot be fetched due to local changes"
                          .format(name))
            raise BuilderStop

        shutil.rmtree(path, ignore_errors=True)
        repo = git.Repo.clone_from(remote.uri, path, progress=RepoProgressPrinter())
        self._repos[name] = repo

    def clone_repos_doit(self):
        """
        Clone all repositories

        :return:
            List of generators used for doit task.
        """
        for remote in RemoteWalker(self._config.remote, self._config.miner.platform):
            yield self._clone_repo_doit(remote)

    def _checkout_repo(self, repo, remote):
        """
        Switch branches or pull it from remote repository

        :param repo:
            Opened GIT repository.
        :param remote:
            Named tuple where following attributes are used:

            - `name` - name of repository
            - `uri` - address of remote git repository
            - `branch` - name of branch
            - `fetch` - if True then fetch+merge is done
        """
        def head_checkout():
            """
            Try to checkout local head to the requested branch or commit
            :return:
                True when checkout was successful or False when branch or commit does not exist
            """
            if remote.branch in repo.heads:
                head = repo.heads[remote.branch]
                head.checkout()
                if remote.fetch:
                    for repo_remote in repo.remotes:
                        repo_remote.pull()
                return True

            for repo_remote in repo.remotes:
                if remote.branch in repo_remote.refs:
                    ref = repo_remote.refs[remote.branch]
                    head = repo.create_head(remote.branch, ref)
                    head.set_tracking_branch(ref)
                    head.checkout()
                    return True
            try:
                # try to detach head to specific commit
                commit = repo.commit(remote.branch)
                repo.git.checkout(commit)
                return True
            except (git.BadName, ValueError):
                return False

            # try to checkout head from local repository when fetch is disabled

        if remote.fetch or not head_checkout():
            # fetch remote repository when fetch is enabled or local checkout wasn't successful
            for repo_remote in repo.remotes:
                repo_remote.fetch()

            # try checkout after remote fetch (it is second attempt when fetch is disabled)
            if not head_checkout():
                logging.error("Cannot checkout branch '{}'".format(remote.branch))
                raise BuilderStop

    def _checkout_repo_doit(self, remote):
        """
        Switch branches or pull it from remote repository

        :param remote:
            Named tuple with information about remote repository.
        """
        name = remote.name

        def get_reference(repo):
            """
            Return reference to local branch or commit when exists otherwise return None
            """
            if remote.branch in repo.heads:
                return repo.heads[remote.branch]
            try:
                return repo.commit(remote.branch)
            except (git.BadName, ValueError):
                return None

        def head_uptodate():
            """
            Check if current local head is the same as requested one
            """
            repo = self._get_repo(name)
            ref = get_reference(repo)
            return ref == repo.head.reference if not repo.head.is_detached else ref == repo.head.commit

        yield {
            'name': name,
            'uptodate': [not remote.fetch, head_uptodate]
        }

        repo = self._get_repo(name)
        self._checkout_repo(repo, remote)

    def checkout_repos_doit(self):
        """
        Fetch and checkout all repositories to specified branch

        :return:
            List of generators used for doit task.
        """
        for remote in RemoteWalker(self._config.remote, self._config.miner.platform):
            yield self._checkout_repo_doit(remote)

    def prepare_feeds_conf_doit(self):
        """
        Prepare LEDE feeds

        It creates `feeds.conf` when it is not present

        :return:
            Generator returning dictionary with dependencies and action for doit task.
        """
        feeds_path = os.path.join(self._working_dir, self.FEEDS_CONF)
        feeds_links = self._config.feeds.links

        yield {
            'targets': [feeds_path],
            'uptodate': [self._config.feeds.create_always != 'yes',
                         config_changed({name: link for name, link in feeds_links.items()})]
        }

        logging.debug("Creating '{}'".format(feeds_path))
        with open(feeds_path, 'w') as feeds_file:
            for feeds_name, feeds_link in feeds_links.items():
                feeds_file.write('src-link {} {}\n'.format(feeds_name, feeds_link))

    def prepare_feeds_update_doit(self):
        """
        Update feeds from all sources

        :return:
            Generator returning dictionary with dependencies and action for doit task.
        """
        feeds_dir = os.path.join(self._working_dir, self.FEEDS_DIR)

        yield {
            'file_dep': [os.path.join(self._working_dir, self.FEEDS_CONF)],
            'targets': [feeds_dir],
            'uptodate': [self._config.feeds.update_always != 'yes']
        }

        # delete all previous feeds files and related configurations
        shutil.rmtree(feeds_dir, ignore_errors=True)
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

        logging.debug('Updating all feeds')
        self._run(os.path.join('scripts', 'feeds'), 'update', '-a')

    def _prepare_feeds_link_doit(self, name, link):
        """
        Install updated feeds

        :param name:
            Feeds name.
        :param link:
            Local link to feeds directory.
        :return:
            Generator returning dictionary with dependencies and action for doit task.
        """
        def config_files_unchanged(task, values):
            """
            Check if configuration files are unchanged

            These files cannot be used as a file dependencies because they are gathered dynamically and previous
            task can modify them (e.g. checkout another branch)
            """
            config_files_key = 'config_files'
            count = 0

            def save_now():
                return {config_files_key: count}
            task.value_savers.append(save_now)

            patterns = [
                ['**', 'Makefile'],
                ['**', 'Config.in'],
                ['*.index']
            ]
            result = True
            for config_file in chain(*(glob.glob(os.path.join(link, *pattern), recursive=True)
                                       for pattern in patterns)):
                result &= check_timestamp_unchanged(config_file)(task, values)
                count += 1

            prev_count = values.get(config_files_key)
            return result and prev_count == count

        yield {
            'name': name,
            'file_dep': [os.path.join(self._working_dir, self.FEEDS_CONF)],
            'uptodate': [self._config.feeds.update_always != 'yes',
                         self._config.feeds.install_always != 'yes',
                         config_files_unchanged]
        }

        logging.debug('Installing feeds {}'.format(name))
        self._run(os.path.join('scripts', 'feeds'), 'update', name)
        self._run(os.path.join('scripts', 'feeds'), 'install', '-a', '-p', name)

    def prepare_feeds_doit(self):
        """
        Update and install all feeds

        :return:
            List of generators used for doit task.
        """
        feeds_links = self._config.feeds.links

        for feeds_name, feeds_link in feeds_links.items():
            yield self._prepare_feeds_link_doit(feeds_name, feeds_link)

    def prepare_default_config_doit(self):
        """
        Initial default configuration

        :return:
            Generator returning dictionary with dependencies and action for doit task.
        """
        yield {
            'uptodate': [run_once]
        }

        logging.debug("Creating default configuration")
        self._run('make', 'defconfig')

    def prepare_config_doit(self):
        """
        Prepare LEDE configuration file

        It sets default configuration specified in the configuration file under `build.config`.
        It also sets paths to Linux and CGMiner external directories in this configuration file.

        :return:
            Generator returning dictionary with dependencies and action for doit task.
        """
        config_src_path, config_dst_path = self._get_config_paths()
        target_config = io.StringIO()

        # generate target configuration
        for config, generator in self.GENERATED_CONFIGS:
            generator and generator(self, target_config, config)

        target_config.seek(0)

        feeds_files = [
            '.config-feeds.in',
            '.packagedeps',
            '.packageinfo',
            '.config-package.in',
            '.packagesubdirs'
        ]

        yield {
            'file_dep': [config_src_path] +
                        [os.path.join(self._tmp_dir, file_name) for file_name in feeds_files],
            'targets': [config_dst_path],
            'uptodate': [config_changed(target_config.getvalue()),
                         self._config.build.config_always != 'yes']
        }

        logging.debug("Copy config from '{}'".format(config_src_path))
        shutil.copy(config_src_path, config_dst_path)

        with open(config_dst_path, 'a') as config_dst_file:
            shutil.copyfileobj(target_config, config_dst_file)

        logging.debug("Creating full configuration file")
        self._run('make', 'defconfig')

    def _prepare_key(self, attribute: str, key_name: str):
        """
        Prepare one build key

        The keys are used for signing packages and sysupgrade tarball.
        When configuration does not contain any key then LEDE generates new one.

        :return:
            Generator returning dictionary with dependencies and action for doit task.
        """
        key_src_path = self._config.build.get('key.' + attribute, None)
        key_dst_path = os.path.join(self._working_dir, key_name)

        yield {
            'name': '{}_key'.format(attribute),
            'uptodate': [not key_src_path or
                         (os.path.exists(key_dst_path) and filecmp.cmp(key_src_path, key_dst_path)),
                         config_changed('user' if key_src_path else 'generated')]
        }

        if key_src_path:
            # copy new key
            logging.debug("Copy {} build key from '{}'".format(attribute, key_src_path))
            shutil.copy(key_src_path, key_dst_path)
        else:
            # delete all base-files directories to force LEDE to generate new build keys
            for base_file_dir in glob.glob('{}/build_dir/target-*/linux-*/base-files'.format(self._working_dir)):
                shutil.rmtree(base_file_dir)
            # delete previous key
            logging.debug("Delete {} build key'".format(attribute, key_src_path))
            if os.path.exists(key_dst_path):
                os.remove(key_dst_path)

    def prepare_keys_doit(self):
        """
        Prepare LEDE build keys

        :return:
            List of generators used for doit task.
        """
        return iter([
            self._prepare_key('secret', self.BUILD_KEY_NAME),
            self._prepare_key('public', self.BUILD_KEY_PUB_NAME)
        ])

    def _config_lede(self):
        """
        Configure LEDE project

        It calls `make menuconfig` and then stores configuration diff to the file specified in `build.config`.
        """
        config_dst_path, config_src_path = self._get_config_paths()

        config_src_time = os.path.getmtime(config_src_path)
        self._run('make', 'menuconfig')
        if os.path.getmtime(config_src_path) == config_src_time:
            logging.info("Configuration file has not been changed")
            return

        logging.info("Saving changes in configuration to '{}'...".format(config_dst_path))
        with open(config_dst_path, 'w') as config_dst:
            # call ./scripts/diffconfig.sh to get configuration diff
            output = self._run(os.path.join('scripts', 'diffconfig.sh'), output=True)
            for line in output.decode('utf-8').splitlines():
                # do not store lines with configuration of external directories
                # this files are automatically generated
                if not any(line.startswith(config) for config, _ in self.GENERATED_CONFIGS):
                    config_dst.write(line)
                    config_dst.write('\n')

    def _config_kernel(self):
        """
        Configure Linux kernel

        It calls `make kernel_menuconfig`. The configuration is stored in the target directory of the LEDE build system.
        """
        self._run('make', 'kernel_menuconfig')

    def clean(self, purge: bool=False):
        """
        Clean all projects or purge them to initial state.

        :param purge:
            If True then use git to clean the whole repository to its initial state.
        """
        logging.info("Start cleaning LEDE build directory...'")
        if not purge:
            self._run('make', 'clean')
        else:
            for name, repo in self._repos.items():
                if not repo:
                    continue
                logging.debug("Purging '{}'".format(name))
                repo.git.clean('-dxf')

    def config(self, kernel: bool=False):
        """
        Configure LEDE project or Linux kernel

        :param kernel:
            If True then Linux kernel configuration is called instead of LEDE configuration.
        """
        if not kernel:
            logging.info("Start LEDE configuration...'")
            self._config_lede()
        else:
            logging.info("Start Linux kernel configuration...'")
            self._config_kernel()

    def build(self, targets=None):
        """
        Build the Miner firmware for current configuration

        It is possible alter build system by following attributes in configuration file:

        - `build.jobs` - number of jobs to run simultaneously (default is `1`)
        - `build.debug` - show all commands during build process (default is `no`)

        :param targets:
            List of targets for build. Target is specified as an alias to real LEDE target.
            The aliases are stored in configuration file under `build.aliases`
        """
        logging.info("Start building LEDE...'")

        # set PATH environment variable
        env_path = self._config.build.get('env_path', None)
        path = env_path and [os.path.abspath(os.path.expanduser(env_path))]

        # prepare arguments for build
        args = ['make', '-j{}'.format(self._config.build.jobs)]
        if self._config.build.verbose == 'yes':
            args.append('V=s')
        if targets:
            aliases = self._config.build.aliases
            args.extend('{}/install'.format(aliases[target]) for target in targets)
        # run make to build whole LEDE
        # set umask to 0022 to fix issue with incorrect root fs access rights
        self._run(args, path=path, init=partial(os.umask, 0o0022))

    def _write_uenv(self, stream, recovery: bool=False):
        """
        Generate content of uEnv.txt to the file stream

        :param stream:
            File stream with write access.
        :param recovery:
            Write also recovery parameters.
        """
        if self._config.uenv.get('mac', 'no') == 'yes':
            stream.write("{}={}\n".format(nand.NET_MAC, self._config.net.mac))

        bool_attributes = (
            'factory_reset',
            'sd_images',
            'sd_boot'
        )
        for attribute in bool_attributes:
            if self._config.uenv.get(attribute, 'no') == 'yes':
                stream.write("{}=yes\n".format(attribute))

    def _mtd_write(self, ssh, image_path: str, device: str, offset: int=0, compress: bool=False, erase: bool=True):
        """
        Write image to remote NAND partition

        :param ssh:
            Connected SSH client.
        :param image_path:
            Path to local image file.
        :param device:
            Name of NAND partition for writing image.
        :param offset:
            Skip the first n bytes.
        :param compress:
            Compress data with gzip before write to NAND.
        :param erase:
            Write first erasing the blocks.
        """
        command = ['mtd']
        if not erase:
            command.append('-n')
        if offset:
            command.extend(('-p', str(offset)))
        command.extend(('write', '-', device))
        with open(image_path, "rb") as image_file, ssh.pipe(command) as remote:
            if compress:
                remote.stdin.write(gzip.compress(image_file.read()))
            else:
                shutil.copyfileobj(image_file, remote.stdin)

    def _get_bitstream_mtd_name(self, index) -> str:
        """
        Return MTD device name for selected firmware

        :param index:
            Index of firmware partition.
        :return:
            String with name of MTD device.
        """
        return self.MTD_BITSTREAM + str(index)

    def _get_bitstream_path(self, platform: str=None) -> str:
        """
        Return path to FPGA bitstream for selected platform

        :param platform:
            Name of selected platform.
            When platform is omitted then platform from current configuration is used.
        :return:
            String with path to FPGA bitstream.
        """
        platform_dir = self._get_repo_path(self.PLATFORM)
        platform_subtarget = self._split_platform()[1]
        return os.path.join(platform_dir, platform_subtarget, 'system.bit')

    @staticmethod
    def _get_firmware_mtd(index) -> str:
        """
        Return MTD device for selected firmware

        :param index:
            Index of firmware partition.
        :return:
            String with path to MTD device.
        """
        return '/dev/mtd' + {1: '7', 2: '8'}.get(index)

    def _write_nand_uboot(self, ssh, image):
        """
        Write SPL and U-Boot to NAND over SSH connection

        :param ssh:
            Connected SSH client.
        :param image:
            Paths to firmware images.
        """
        boot_images = (
            (image.boot, 'boot'),
            (image.uboot, 'uboot')
        )
        for local, mtd in boot_images:
            logging.info("Writing '{}' to NAND partition '{}'...".format(os.path.basename(local), mtd))
            self._mtd_write(ssh, local, mtd)

    def _upload_images(self, upload_manager, image, recovery: bool=False, compressed=()):
        """
        Upload all image files using upload manager

        :param upload_manager:
            Upload manager for images transfer.
        :param image:
            Paths to firmware images.
        :param recovery:
            Transfer recovery images.
        :param compressed:
            List of images which should be compressed.
        """
        upload = [
            (image.boot, 'boot.bin'),
            (image.uboot, 'u-boot.img'),
            (image.fpga, 'system.bit'),
            (image.kernel, 'fit.itb')
        ]
        if recovery:
            upload.append((image.factory, 'factory.bin'))

        for local, remote in upload:
            compress = remote in compressed
            if compress:
                remote += '.gz'
            upload_manager.put(local, remote, compress)

    def _deploy_ssh_sd(self, ssh, sftp, image, recovery: bool):
        """
        Deploy image to the SD card over SSH connection

        :param ssh:
            Connected SSH client.
        :param sftp:
            Opened SFTP connection by SSH client.
        :param image:
            Paths to firmware images.
        :param recovery:
            Transfer recovery images.
        """
        class UploadManager:
            def __init__(self, sftp):
                self.sftp = sftp

            def put(self, src, dst, compress=False, cache=None):
                logging.info("Uploading '{}'...".format(dst))
                self.sftp.put(src, dst)

        ssh.run('mount', '/dev/mmcblk0p1', '/mnt')
        sftp.chdir('/mnt')

        # start uploading
        self._upload_images(UploadManager(sftp), image, recovery)

        ssh.run('umount', '/mnt')

    def _deploy_ssh_nand_recovery(self, ssh, image):
        """
        Deploy image to the NAND recovery over SSH connection

        It is required that remote system has been booted from SD card or recovery partition!

        :param ssh:
            Connected SSH client.
        :param image:
            Paths to firmware images.
        """
        mtd_name = 'recovery'

        self._write_nand_uboot(ssh, image)

        # erase device before formating
        ssh.run('mtd', 'erase', mtd_name)

        local = image.kernel
        logging.info("Writing '{}' to NAND partition '{}'..."
                     .format(os.path.basename(local), mtd_name))
        self._mtd_write(ssh, local, mtd_name)

        local = image.factory
        logging.info("Writing '{}' to NAND partition '{}'..."
                     .format(os.path.basename(local), mtd_name))
        self._mtd_write(ssh, local, mtd_name, offset=0x800000, compress=True, erase=False)

        local = image.fpga
        logging.info("Writing '{}' to NAND partition '{}'..."
                     .format(os.path.basename(local), mtd_name))
        self._mtd_write(ssh, local, mtd_name, offset=0x1400000, compress=True, erase=False)

        local = image.boot
        logging.info("Writing '{}' to NAND partition '{}'..."
                     .format(os.path.basename(local), mtd_name))
        self._mtd_write(ssh, local, mtd_name, offset=0x1500000, compress=True, erase=False)

    def _deploy_ssh_nand(self, ssh, image):
        """
        Deploy image to the NAND over SSH connection

        It is required that remote system has been booted from SD card or recovery partition!

        :param ssh:
            Connected SSH client.
        :param image:
            Paths to firmware images.
        """
        platform = self._config.miner.platform

        self._write_nand_uboot(ssh, image)

        firmwares = (
            ('nand_firmware1', 1),
            ('nand_firmware2', 2)
        )
        targets = self._config.deploy.targets

        if self._config.deploy.write_bitstream == 'yes':
            mtds = (self._get_bitstream_mtd_name(i) for name, i in firmwares if name in targets)
            for mtd_name in mtds:
                logging.info("Writing bitstream for platform '{}' to NAND partition '{}'..."
                             .format(platform, mtd_name))
                self._mtd_write(ssh, image.fpga, mtd_name, compress=True)

        mtds = ((name[5:], self._get_firmware_mtd(i)) for name, i in firmwares if name in targets)
        for firmware, mtd in mtds:
            if self._config.deploy.factory_image == 'yes':
                logging.info("Formating '{}' ({}) with 'factory.bin'...".format(firmware, mtd))
                # erase device before formating
                ssh.run('mtd', 'erase', mtd)
                # use factory image which deletes overlay data from UBIFS
                image_size = os.path.getsize(image.factory)
                with open(image.factory, "rb") as image_file:
                    with ssh.pipe('ubiformat', mtd, '-f', '-', '-S', str(image_size)) as remote:
                        shutil.copyfileobj(image_file, remote.stdin)
            else:
                logging.info("Updating '{}' ({}) volumes with 'sysupgrade.tar'...".format(firmware, mtd))
                # use sysupgrade image which preserves overlay data from UBIFS
                ssh.run('ubiattach', '-p', mtd)
                volume_images = (
                    ('kernel', 'sysupgrade-miner-nand/kernel', '/dev/ubi0_0'),
                    ('rootfs', 'sysupgrade-miner-nand/root', '/dev/ubi0_1')
                )
                for volume_name, volume_image, device in volume_images:
                    logging.info("Updating volume '{}' ({}) with '{}'...".format(volume_name, device, volume_image))
                    with tarfile.open(image.sysupgrade, 'r') as sysupgrade_file:
                        image_info = sysupgrade_file.getmember(volume_image)
                        image_file = sysupgrade_file.extractfile(image_info)
                        with ssh.pipe('ubiupdatevol', device, '-', '-s', str(image_info.size)) as remote:
                            shutil.copyfileobj(image_file, remote.stdin)
                ssh.run('ubidetach', '-p', mtd)

    def _config_ssh_sd(self, ssh, sftp, recovery: bool):
        """
        Change configuration on SD card over SSH connection

        :param ssh:
            Connected SSH client.
        :param sftp:
            Opened SFTP connection by SSH client.
        :param recovery:
            Use options for recovery image.
        """
        reset_extroot = self._config.deploy.reset_extroot == 'yes'
        remove_extroot_uuid = self._config.deploy.remove_extroot_uuid == 'yes'

        # create uEnv.txt for U-Boot external configuration
        ssh.run('mount', '/dev/mmcblk0p1', '/mnt')
        sftp.chdir('/mnt')

        logging.info("Creating '{}'...".format(self.UENV_TXT))
        with sftp.open(self.UENV_TXT, 'w') as file:
            self._write_uenv(file, recovery)

        ssh.run('umount', '/mnt')

        # delete the whole extroot or delete extroot UUID
        if reset_extroot or remove_extroot_uuid:
            ssh.run('mount', '/dev/mmcblk0p2', '/mnt')
            sftp.chdir('/mnt')

            if reset_extroot:
                logging.info("Removing all data from extroot...")
                ssh.run('rm', '-fr', '/mnt/*')
            elif '.extroot-uuid' in sftp.listdir('etc'):
                logging.info("Removing extroot UUID...")
                sftp.remove('etc/.extroot-uuid')

            ssh.run('umount', '/mnt')

    def _config_ssh_nand(self, ssh):
        """
        Change configuration on NAND over SSH connection

        :param ssh:
            Connected SSH client.
        """
        # write miner configuration to miner_cfg NAND
        if self._config.deploy.write_miner_cfg == 'yes':
            miner_cfg_input = io.BytesIO()
            if not nand.write_miner_cfg_input(self._config, miner_cfg_input):
                raise BuilderStop
            # generate image file with NAND configuration
            mkenvimage = self._get_utility(self.LEDE_MKENVIMAGE)
            output = self._run(mkenvimage, '-r', '-p', str(0), '-s', str(nand.MINER_CFG_SIZE), '-',
                               input=miner_cfg_input.getvalue(), output=True)
            logging.info("Writing miner configuration to NAND partition 'miner_cfg'...")
            with ssh.pipe('mtd', 'write', '-', 'miner_cfg') as remote:
                remote.stdin.write(output)

        # change miner configuration in U-Boot env
        if self._config.deploy.set_miner_env == 'yes' and self._config.deploy.reset_uboot_env == 'no':
            logging.info("Writing miner configuration to U-Boot env in NAND...")
            ssh.run('fw_setenv', nand.NET_MAC, self._config.net.mac)
            ssh.run('fw_setenv', nand.MINER_HWID, self._config.miner.hwid)
            ssh.run('fw_setenv', nand.MINER_FIRMWARE, str(self._config.miner.firmware))

        reset_uboot_env = self._config.deploy.reset_uboot_env == 'yes'
        reset_overlay = self._config.deploy.reset_overlay == 'yes'

        ubi_attach = reset_overlay

        if ubi_attach:
            firmware_mtd = self._get_firmware_mtd(self._config.miner.firmware)
            ssh.run('ubiattach', '-p', firmware_mtd)

        if reset_uboot_env:
            logging.info("Erasing NAND partition 'uboot_env'...")
            ssh.run('mtd', 'erase', 'uboot_env')

        # truncate overlay for current firmware
        if reset_overlay:
            logging.info("Truncating UBI volume 'rootfs_data'...")
            ssh.run('ubiupdatevol', '/dev/ubi0_2', '-t')

        if ubi_attach:
            ssh.run('ubidetach', '-p', firmware_mtd)

    def _deploy_ssh(self, images, sd_config: bool, nand_config: bool):
        """
        Deploy NAND or SD card image over SSH connection

        It can also change configuration in NAND and SD card.

        :param images:
            List of images for deployment.
            It is also possible to provide empty list and alter only miner configuration:

            - change MAC and HW ID in U-Boot env
            - erase NAND partitions to set it to the default state
            - remove extroot UUID
            - overwrite miner configuration with new MAC or HW ID
        :param sd_config:
            Modify configuration files on SD card.
        :param nand_config:
            Modify configuration files/partitions on NAND.
        """
        hostname = self._config.deploy.ssh.get('hostname', None) or self._config.net.get('hostname', None)
        password = self._config.deploy.ssh.get('password', None)
        username = self._config.deploy.ssh.username

        if not hostname:
            # when hostname is not set, use standard name derived from MAC address
            hostname_suffix = self._config.deploy.ssh.get('hostname_suffix', '')
            hostname = self._get_hostname() + hostname_suffix

        with SSHManager(hostname, username, password) as ssh:
            sftp = ssh.open_sftp()

            image_sd = images.get('sd')
            image_nand_recovery = images.get('nand_recovery')
            image_nand = images.get('nand')

            sd_recovery = image_sd and isinstance(image_sd, ImageRecovery)

            if image_sd:
                self._deploy_ssh_sd(ssh, sftp, image_sd, sd_recovery)
            if sd_config:
                self._config_ssh_sd(ssh, sftp, sd_recovery)
            if image_nand_recovery:
                self._deploy_ssh_nand_recovery(ssh, image_nand_recovery)
            if image_nand:
                self._deploy_ssh_nand(ssh, image_nand)
            if nand_config:
                self._config_ssh_nand(ssh)

            # reboot system if requested
            if self._config.deploy.reboot == 'yes':
                ssh.run('reboot')

            sftp.close()

    def _get_local_target_dir(self, dir_name: str):
        """
        Return path to local target directory

        :param dir_name:
            Name of target directory.
        :return:
            Path to target directory.
        """
        target_dir = self._config.local.get(dir_name, None)
        if not target_dir:
            logging.error("Missing path for local target '{}'".format(dir_name))
            raise BuilderStop

        # prepare target directory
        target_dir = os.path.abspath(target_dir)
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)
        return target_dir

    def _write_local_uenv(self, dir_name: str, recovery: bool=False):
        """
        Create uEnv.txt file in target directory with specific parameters

        :param dir_name:
            Name of target directory.
        :param recovery:
            Write also recovery parameters.
        """
        target_dir = self._get_local_target_dir(dir_name)
        with open(os.path.join(target_dir, self.UENV_TXT), 'w') as target_file:
            logging.info("Creating '{}' in '{}'...".format(self.UENV_TXT, target_dir))
            self._write_uenv(target_file, recovery)

    @staticmethod
    def _get_project_file(*path):
        """
        Return absolute path to the file from project directory

        :param name:
            Relative path to the file.
        :return:
            Path to the file from project directory or None when file does not exist.
        """
        file_path = os.path.abspath(os.path.join(*path))
        return file_path if os.path.isfile(file_path) else None

    def _create_upgrade_miner_cfg_input(self):
        """
        Create input source for mkenvimage with miner configuration
        The configuration does not include MAC and HWID information.

        :return:
            Bytes stream with miner configuration.
        """
        miner_cfg_input = io.BytesIO()
        if not nand.write_miner_cfg_input(self._config, miner_cfg_input, {nand.NET_MAC, nand.MINER_HWID}):
            raise BuilderStop
        return miner_cfg_input

    def _create_upgrade_uboot_env(self):
        """
        Create U-Boot environment for converted Dm firmware

        :return:
            Bytes stream with U-Boot environment.
        """
        mkenvimage = self._get_utility(self.LEDE_MKENVIMAGE)
        uboot_env_base_input = self._get_project_file(self.UPGRADE_DIR, self.UPGRADE_UBOOT_ENV_TXT)
        uboot_env_input = self._create_upgrade_miner_cfg_input()

        # merge miner configuration with default U-Boot env
        with open(uboot_env_base_input, 'rb') as base_input_file:
            shutil.copyfileobj(base_input_file, uboot_env_input)

        return io.BytesIO(
            self._run(mkenvimage, '-r', '-p', str(0), '-s', str(nand.MINER_ENV_SIZE), '-',
                      input=uboot_env_input.getvalue(), output=True)
        )

    def _create_upgrade_miner_cfg(self):
        """
        Create empty miner configuration environment

        :return:
            Bytes stream with miner configuration environment.
        """
        mkenvimage = self._get_utility(self.LEDE_MKENVIMAGE)
        miner_cfg_input = self._create_upgrade_miner_cfg_input()

        return io.BytesIO(
            self._run(mkenvimage, '-r', '-p', str(0), '-s', str(nand.MINER_CFG_SIZE), '-',
                      input=miner_cfg_input.getvalue(), output=True)
        )

    def _add2tar_compressed_file(self, tar, file_path, arcname):
        """
        Add to opened tar compressed file

        :param tar:
            Opened tar for writing.
        :param file_path:
            Path to uncompressed file.
        :param arcname:
            Name of file in the archive.
        """
        file_info = tar.gettarinfo(file_path, arcname=arcname)

        with open(file_path, "rb") as image_file:
            compressed_file = gzip.compress(image_file.read())
            file_info.size = len(compressed_file)
            compressed_file = io.BytesIO(compressed_file)

        tar.addfile(file_info, compressed_file)

    def _create_upgrade_stage2(self, image):
        """
        Create tarball with images for stage2 upgrade

        :param image:
            Paths to firmware images.
        """
        logging.info("Creating upgrade stage2 tarball...")

        stage2 = io.BytesIO()
        tar = tarfile.open(mode = "w:gz", fileobj=stage2)

        # add recovery image
        tar.add(image.kernel_recovery, arcname='fit.itb')

        # add compressed system.bin and factory.bin
        self._add2tar_compressed_file(tar, image.boot, 'boot.bin.gz')
        self._add2tar_compressed_file(tar, image.fpga, 'system.bit.gz')
        self._add2tar_compressed_file(tar, image.factory, 'factory.bin.gz')

        # add miner_cfg.config file
        miner_cfg_config = self._get_project_file(self.UPGRADE_DIR, self.UPGRADE_MINER_CFG_CONFIG)
        tar.add(miner_cfg_config, self.UPGRADE_MINER_CFG_CONFIG)

        # add miner configuration environment compatible with U-Boot
        miner_cfg = self._create_upgrade_miner_cfg()
        miner_cfg_info = tar.gettarinfo(miner_cfg_config, arcname=self.UPGRADE_MINER_CFG)
        miner_cfg_info.size = get_stream_size(miner_cfg)
        tar.addfile(miner_cfg_info, miner_cfg)

        # add upgrade script
        upgrade = self._get_project_file(self.UPGRADE_DIR, self.UPGRADE_STAGE2_SCRIPT)
        tar.add(upgrade, self.UPGRADE_STAGE2_SCRIPT)

        tar.close()
        stage2.seek(0)
        return stage2

    def _get_upgrade_file(self, name, version):
        """
        Return absolute path to upgrade file

        It tries to find file with matching version or continues to find more generic implementation.

        :param name:
            Name of upgrade file.
        :param version:
            Version of target firmware.
        :return:
            Absolute path to upgrade file or None when it does not exist.
        """
        # try file paths from the most specific to more generic one
        relative_paths = [
            os.path.join(*version, name),
            os.path.join(version[0], name),
            name
        ]
        for relative_path in relative_paths:
            path = self._get_project_file(self.UPGRADE_DIR, relative_path)
            if path:
                return path
        return None

    def _create_upgrade_stage1_control(self, version):
        """
        Create script with variables for stage1 upgrade script

        :param version:
            Version of target firmware.
        :return:
            Opened stream with generated script.
        """
        control_path = self._get_upgrade_file(self.UPGRADE_STAGE1_CONTROL_SRC, version)

        info = io.BytesIO()
        hwver = {
            'zynq-dm1-g9': 'G9',
            'zynq-dm1-g19': 'G19',
            'zynq-am1-s9': 'S9'
        }.get(self._config.miner.platform)
        info.write('FW_MINER_HWVER="{}"\n\n'.format(hwver).encode())

        with open(control_path, 'rb') as control_file:
            shutil.copyfileobj(control_file, info)

        info.seek(0)
        return info

    def _deploy_local_upgrade(self, upload_manager, image, version):
        """
        Deploy NAND or SD card image for Dm upgrade to local file system

        :param upload_manager:
            Upload manager for images transfer.
        :param image:
            Paths to firmware images.
        :param version:
            Version of target firmware.
        """
        # copy all files for transfer to subdirectory
        upload_manager.push_dir(self.UPGRADE_FIRMWARE_DIR)

        self._upload_images(upload_manager, image, compressed=('system.bit',))

        # copy uboot_env.config file
        uboot_env_config = self._get_project_file(self.UPGRADE_DIR, self.UPGRADE_UBOOT_ENV_CONFIG)
        upload_manager.put(uboot_env_config, self.UPGRADE_UBOOT_ENV_CONFIG)

        # create U-Boot environment
        uboot_env = self._create_upgrade_uboot_env()
        upload_manager.put(uboot_env, self.UPGRADE_UBOOT_ENV)

        # create tar with images for stage2 upgrade
        stage2 = None
        while not upload_manager.put(stage2, self.UPGRADE_STAGE2, cache=self.UPGRADE_STAGE2):
            stage2 = self._create_upgrade_stage2(image)

        # create env.sh with script variables
        stage1_env = self._create_upgrade_stage1_control(version)
        upload_manager.put(stage1_env, self.UPGRADE_STAGE1_CONTROL)

        # copy stage1 upgrade script
        upgrade = self._get_project_file(self.UPGRADE_DIR, self.UPGRADE_STAGE1_SCRIPT)
        upload_manager.put(upgrade, self.UPGRADE_STAGE1_SCRIPT)

        # change to original target directory
        upload_manager.pop_dir()

        # copy system dependencies
        if version[0] == self.UPGRADE_AM1:
            build_dir = os.path.join(self._working_dir, 'build_dir', 'target-arm_cortex-a9+neon_musl-1.1.16_eabi')
            upload_manager.push_dir('system')

            upload_manager.put(os.path.join(build_dir, 'toolchain', 'ipkg-arm_cortex-a9_neon', 'libc', 'lib',
                                            'ld-musl-armhf.so.1'), 'ld-musl-armhf.so.1')
            upload_manager.put(os.path.join(build_dir, 'u-boot-2018.03', 'ipkg-arm_cortex-a9_neon', 'uboot-envtools',
                                            'usr', 'sbin', 'fw_printenv'), 'fw_printenv')
            if version != self.UPGRADE_AM1_WEB:
                upload_manager.put(os.path.join(build_dir, 'openssh-without-pam', 'openssh-7.4p1',
                                                'sftp-server'), 'sftp-server')
            upload_manager.pop_dir()

        # copy upgrade scripts
        if version == self.UPGRADE_AM1_WEB:
            runme = self._get_upgrade_file(self.UPGRADE_AM_RUNME_SRC, version)
            upload_manager.put(runme, self.UPGRADE_AM_RUNME)
            ubi_info = self._get_upgrade_file(self.UPGRADE_AM_UBI_INFO_SRC, version)
            upload_manager.put(ubi_info, self.UPGRADE_AM_UBI_INFO)
        else:
            # copy upgrade modules
            upload_manager.push_dir('upgrade')

            init = self._get_upgrade_file(self.UPGRADE_INIT_SCRIPT_SRC, version)
            upload_manager.put(init, self.UPGRADE_INIT_SCRIPT)
            transfer = self._get_upgrade_file(self.UPGRADE_TRANSFER_SCRIPT_SRC, version)
            upload_manager.put(transfer, self.UPGRADE_TRANSFER_SCRIPT)
            hwid = self._get_project_file(self.LEDE_META_DIR, self.LEDE_META_HWID)
            upload_manager.put(hwid, self.LEDE_META_HWID)
            ssh = self._get_project_file(self.LEDE_META_DIR, self.LEDE_META_SSH)
            upload_manager.put(ssh, self.LEDE_META_SSH)
            platform = self._get_upgrade_file(self.UPGRADE_PLATFORM_SCRIPT_SRC, version)
            upload_manager.put(platform, self.UPGRADE_PLATFORM_SCRIPT)
            backup = self._get_upgrade_file(self.UPGRADE_BACKUP_SCRIPT_SRC, version)
            upload_manager.put(backup, self.UPGRADE_BACKUP_SCRIPT)

            # get optional factory restore script
            factory_restore = self._get_upgrade_file(self.UPGRADE_FACTORY_RESTORE_SRC, version)
            if factory_restore:
                upload_manager.put(factory_restore, self.UPGRADE_FACTORY_RESTORE)

            upload_manager.pop_dir()

            # copy main scripts
            upgrade = self._get_upgrade_file(self.UPGRADE_SCRIPT_SRC, version)
            upload_manager.put(upgrade, self.UPGRADE_SCRIPT)
            restore = self._get_upgrade_file(self.UPGRADE_RESTORE_SCRIPT_SRC, version)
            upload_manager.put(restore, self.UPGRADE_RESTORE_SCRIPT)

            requirements = self._get_upgrade_file(self.UPGRADE_SCRIPT_REQUIREMENTS_SRC, version)
            upload_manager.put(requirements, self.UPGRADE_SCRIPT_REQUIREMENTS)

    def _deploy_local_upgrades(self, upload_manager_cls, target_dir, image):
        """
        Deploy all versions of upgrade images

        :param upload_manager_cls:
            Upload manager class for images transfer.
        :param target_dir:
            Path to target directory.
        :param image:
            Paths to firmware images.
        """
        @contextmanager
        def get_dst_path(temporary: bool):
            if temporary:
                directory = tempfile.TemporaryDirectory()
                yield directory.name
                directory.cleanup()
            else:
                yield target_dir

        cache = None
        versions = next((value for pattern, value in sorted(self.UPGRADE_VERSION.items(), reverse=True)
                         if self._config.miner.platform.startswith(pattern)), None)

        for version, (archive, archive_flags) in versions:
            # create subdirectory for specific version
            subtarget_path = '{}_{}_{}_{}'.format(
                self.UPGRADE_IMAGE_PREFIX,
                self._split_platform()[1],
                version[1],
                self.get_firmware_version())

            with get_dst_path(archive is not None) as dst_path:
                upload_manager = upload_manager_cls(dst_path, cache=cache)
                if self.ARCHIVE_FLAG_FLAT not in archive_flags:
                    upload_manager.push_dir(subtarget_path)

                # prepare local image for potential archiving
                self._deploy_local_upgrade(upload_manager, image, version)

                # archive result
                if archive in [self.ARCHIVE_TGZ, self.ARCHIVE_TBZ2]:
                    dst_file_path = os.path.join(target_dir, subtarget_path) + '.' + archive
                    mode = "w:{}".format(archive.split('.')[1])
                    with tarfile.open(dst_file_path, mode) as tar:
                        for file_path in os.listdir(dst_path):
                            tar.add(os.path.join(dst_path, file_path), arcname=file_path)

            # use cache for next run to get same shared objects
            cache = upload_manager.get_cache()

    def _deploy_local(self, images, sd_config: bool, sd_recovery_config: bool):
        """
        Deploy NAND or SD card image to local file system

        It can also generate configuration files for SD card version.

        :param images:
            List of images for deployment.
        :param sd_config:
            Generate configuration files for SD card version.
        :param sd_recovery_config:
            Generate configuration files for recovery SD card version.
        """
        class UploadManager:
            def __init__(self, target_dir: str, cache=None):
                self._target_dir_prev = []
                self._target_dir = target_dir
                self._cache = cache or {}

            def get_cache(self):
                return self._cache

            def push_dir(self, path):
                self._target_dir_prev.append(self._target_dir)
                self._target_dir = os.path.join(self._target_dir, path)
                os.makedirs(self._target_dir, exist_ok=True)

            def pop_dir(self):
                self._target_dir = self._target_dir_prev.pop()

            def put(self, src, dst, compress=False, cache: str=None):
                src_path = type(src) is str
                if cache and not src:
                    src_file = self._cache.get(cache)
                    if not src_file:
                        # file is not found in cache
                        return False
                    src_file.seek(0)
                else:
                    src_file = open(src, 'rb') if src_path else src
                    if cache and not src_path:
                        self._cache[cache] = src_file
                logging.info("Copying '{}' to '{}'...".format(dst, self._target_dir))
                dst_file = open(os.path.join(self._target_dir, dst), 'wb')
                if compress:
                    # set gzip to get reproducible output
                    dst_file = gzip.GzipFile(filename='', mode='wb', fileobj=dst_file, mtime=0)
                shutil.copyfileobj(src_file, dst_file)
                dst_file.close()

                if src_path:
                    src_file.close()
                return True

        image_sd = images.get('sd')
        image_sd_recovery = images.get('sd_recovery')
        image_nand_recovery = images.get('nand_recovery')
        image_upgrade = images.get('upgrade')

        if image_sd:
            target_dir = self._get_local_target_dir('sd')
            self._upload_images(UploadManager(target_dir), image_sd)
        if sd_config:
            self._write_local_uenv('sd_config')
        if image_sd_recovery:
            target_dir = self._get_local_target_dir('sd_recovery')
            self._upload_images(UploadManager(target_dir), image_sd_recovery, recovery=True)
        if sd_recovery_config:
            self._write_local_uenv('sd_recovery_config', recovery=True)

        if image_nand_recovery:
            target_dir = self._get_local_target_dir('nand_recovery')
            self._upload_images(UploadManager(target_dir), image_nand_recovery, recovery=True)

        if image_upgrade:
            target_dir = self._get_local_target_dir('upgrade')
            self._deploy_local_upgrades(UploadManager, target_dir, image_upgrade)

    def _deploy_feeds(self, images):
        """
        Deploy package feeds to local file system

        :param images:
            List of images for deployment.
        """
        local_feeds = images.get('local')
        target_dir = self._get_local_target_dir('feeds')

        src_feeds_index = os.path.join(local_feeds.packages, self.FEEDS_INDEX)
        dst_feeds_index = os.path.join(target_dir, self.FEEDS_INDEX)

        # find package firmware meta information
        with Packages(src_feeds_index) as src_packages:
            firmware_package = next((package for package in src_packages
                                     if package[self.FEEDS_ATTR_PACKAGE] == self.FEED_FIRMWARE), None)
        if not firmware_package:
            logging.error("Missing firmware package in '{}'".format(src_feeds_index))
            raise BuilderStop

        # prepare base feeds index
        feeds_base = None
        feeds_base_url = self._config.deploy.get('feeds_base', None)
        fw_require = self._get_sysupgrade_attr(self.SYSUPGRADE_ATTR_REQUIRE)

        if feeds_base_url:
            # appending to previous index
            feeds_base = urlopen(Request(feeds_base_url, headers={'User-Agent': 'Mozilla/5.0'}))
            feeds_base = feeds_base.read().decode('utf-8')

        # create destination feeds index
        with open(dst_feeds_index, 'w') as dst_packages:
            if feeds_base:
                dst_packages.write(feeds_base)
                dst_packages.write('\n')
            for attribute, value in firmware_package.items():
                if attribute not in self.FEEDS_EXCLUDED_ATTRIBUTES:
                    dst_packages.write('{}: {}\n'.format(attribute, value))
                if fw_require and attribute == self.FEEDS_ATTR_VERSION:
                    # insert previous firmware requirements after version attribute
                    dst_packages.write('{}: {}\n'.format(self.FEEDS_ATTR_REQUIRE, fw_require))

        # sign the created index file
        usign = self._get_utility(self.LEDE_USIGN)
        self._run(usign, '-S', '-m', dst_feeds_index, '-s', local_feeds.key)

        # compress signed index file
        with open(dst_feeds_index, 'rb') as file_in, gzip.open(dst_feeds_index + '.gz', 'wb') as file_out:
            shutil.copyfileobj(file_in, file_out)

        # copy firmware packages
        firmware_ipk = firmware_package[self.FEEDS_ATTR_FILENAME]
        src_package = os.path.join(local_feeds.packages, firmware_ipk)
        dst_sysupgrade = os.path.join(target_dir, os.path.splitext(firmware_ipk)[0] + '.tar')

        shutil.copy(src_package, target_dir)
        shutil.copy(local_feeds.sysupgrade, dst_sysupgrade)

    def _get_recovery_image(self, platform: str, generic_dir: str, uboot_dir: str):
        """
        Return recovery image for SD or NAND version

        :param platform:
            Name of platform.
        :param generic_dir:
            Path to LEDE output target directory.
        :param uboot_dir:
            Relative path to output U-Boot directory.
        :return:
            Recovery image with all image files.
        """
        return ImageRecovery(
                    boot=os.path.join(generic_dir, uboot_dir, 'boot.bin'),
                    uboot=os.path.join(generic_dir, uboot_dir, 'u-boot.img'),
                    fpga=self._get_bitstream_path(),
                    kernel=os.path.join(generic_dir, 'lede-{}-recovery-squashfs-fit.itb'.format(platform)),
                    factory=os.path.join(generic_dir, 'lede-{}-nand-squashfs-factory.bin'.format(platform))
                )

    def deploy(self):
        """
        Deploy Miner firmware to target platform
        """
        platform = self._config.miner.platform
        platform_target, _ = self._split_platform(platform)
        targets = self._config.deploy.targets

        logging.info("Start deploying Miner firmware...")

        generic_dir = os.path.join(self._working_dir, 'bin', 'targets', 'zynq')

        supported_targets = [
            'sd_config',
            'sd',
            'sd_recovery',
            'nand_config',
            'nand_recovery',
            'nand_firmware1',
            'nand_firmware2',
            'local_sd_config',
            'local_sd_recovery_config',
            'local_nand_recovery',
            'local_upgrade',
            'local_feeds'
        ]
        aliased_targets = {
            'nand': {
                'targets': {'nand_recovery', 'nand_config'},
                'configs': (('write_miner_cfg', 'yes'), ('reset_uboot_env', 'yes'), ('reboot', 'yes'))
            },
            'local_sd': {
                'targets': {'local_sd', 'local_sd_config'},
            },
            'local_sd_recovery': {
                'targets': {'local_sd_recovery', 'local_sd_recovery_config'},
            }
        }

        images_ssh = {}
        images_local = {}
        images_feeds = {}

        if targets:
            # expand aliased targets
            expanded_targets = set()
            for target in targets:
                aliased_target = aliased_targets.get(target)
                if aliased_target:
                    expanded_targets.update(aliased_target['targets'])
                    for config, value in aliased_target.get('configs') or []:
                        setattr(self._config.deploy, config, value)
                elif target not in supported_targets:
                    logging.error("Unsupported target '{}' for firmware image".format(target))
                    raise BuilderStop
                else:
                    expanded_targets.add(target)

            targets = expanded_targets

            if all(target in targets for target in ('sd', 'sd_recovery')):
                logging.error("Targets 'sd' and 'sd_recovery' are mutually exclusive")
                raise BuilderStop

            if any(target in targets for target in ('sd', 'local_sd')):
                uboot_dir = 'uboot-{}-sd'.format(platform)
                sd = ImageSd(
                    boot=os.path.join(generic_dir, uboot_dir, 'boot.bin'),
                    uboot=os.path.join(generic_dir, uboot_dir, 'u-boot.img'),
                    fpga=self._get_bitstream_path(),
                    kernel=os.path.join(generic_dir, 'lede-{}-sd-squashfs-fit.itb'.format(platform))
                )
                if 'sd' in targets:
                    images_ssh['sd'] = sd
                if 'local_sd' in targets:
                    images_local['sd'] = sd
            if any(target in targets for target in ('sd_recovery', 'local_sd_recovery')):
                uboot_dir = 'uboot-{}-sd'.format(platform)
                sd_recovery = self._get_recovery_image(platform, generic_dir, uboot_dir)
                if 'sd_recovery' in targets:
                    images_ssh['sd'] = sd_recovery
                if 'local_sd_recovery' in targets:
                    images_local['sd_recovery'] = sd_recovery
            if any(target in targets for target in ('nand_recovery', 'local_nand_recovery')):
                uboot_dir = 'uboot-{}'.format(platform)
                nand_recovery = self._get_recovery_image(platform, generic_dir, uboot_dir)
                if 'nand_recovery' in targets:
                    images_ssh['nand_recovery'] = nand_recovery
                if 'local_nand_recovery' in targets:
                    images_local['nand_recovery'] = nand_recovery
            if any(target in targets for target in ('nand_firmware1', 'nand_firmware2')):
                uboot_dir = 'uboot-{}'.format(platform)
                images_ssh['nand'] = ImageNand(
                    boot=os.path.join(generic_dir, uboot_dir, 'boot.bin'),
                    uboot=os.path.join(generic_dir, uboot_dir, 'u-boot.img'),
                    fpga=self._get_bitstream_path(),
                    factory=os.path.join(generic_dir, 'lede-{}-nand-squashfs-factory.bin'.format(platform)),
                    sysupgrade=os.path.join(generic_dir, 'lede-{}-nand-squashfs-sysupgrade.tar'.format(platform))
                )
            if 'local_upgrade' in targets:
                uboot_dir = 'uboot-{}'.format(platform)
                upgrade = ImageUpgrade(
                    boot=os.path.join(generic_dir, uboot_dir, 'boot.bin'),
                    uboot=os.path.join(generic_dir, uboot_dir, 'u-boot.img'),
                    fpga=self._get_bitstream_path(),
                    kernel=os.path.join(generic_dir, 'lede-{}-upgrade-squashfs-fit.itb'.format(platform)),
                    kernel_recovery=os.path.join(generic_dir, 'lede-{}-recovery-squashfs-fit.itb'.format(platform)),
                    factory=os.path.join(generic_dir, 'lede-{}-nand-squashfs-factory.bin'.format(platform))
                )
                images_local['upgrade'] = upgrade
            if 'local_feeds' in targets:
                feeds = ImageFeeds(
                    key=os.path.join(self._working_dir, self.BUILD_KEY_NAME),
                    packages=os.path.join(self._working_dir, 'staging_dir', 'packages', platform_target),
                    sysupgrade=os.path.join(generic_dir, 'lede-{}-nand-squashfs-sysupgrade.tar'.format(platform))
                )
                images_feeds['local'] = feeds

        sd_config = 'sd_config' in targets
        nand_config = 'nand_config' in targets

        sd_config_local = 'local_sd_config' in targets
        sd_recovery_config = 'local_sd_recovery_config' in targets

        if images_ssh or sd_config or nand_config:
            self._deploy_ssh(images_ssh, sd_config, nand_config)
        if images_local or sd_config_local or sd_recovery_config:
            self._deploy_local(images_local, sd_config_local, sd_recovery_config)
        if images_feeds:
            self._deploy_feeds(images_feeds)

    @staticmethod
    def _count_commits(repo, branch_name=None):
        """
        Count commits difference among current branch and remote branch

        :param repo:
            Initialized repository object.
        :param branch_name:
            Name of branch to compare or None when current branch should be used.
        :return:
            Pair with number of commits ahead and behind remote branch.
        """
        # get current branch name if not specified
        branch_name = branch_name or repo.active_branch.name
        commits_ahead = sum(1 for _ in repo.iter_commits('{0}@{{u}}..{0}'.format(branch_name)))
        commits_behind = sum(1 for _ in repo.iter_commits('{0}..{0}@{{u}}'.format(branch_name)))
        return commits_ahead, commits_behind

    def status(self):
        """
        Show status of all repositories

        It is equivalent of `git status` and shows all changes in related projects.
        """
        def get_diff_path(diff):
            if diff.change_type[0] == 'R':
                return '{} -> {}'.format(diff.a_path, diff.b_path)
            else:
                return diff.a_path

        for name, repo in self._repos.items():
            if not repo:
                logging.warning("Status for '{}'".format(name))
                print('missing or corrupted repository')
                print()
                continue

            working_dir = os.path.relpath(repo.working_dir, os.getcwd())
            branch_name = repo.active_branch.name if not repo.head.is_detached else \
                'HEAD detached at {}'.format(repo.head.object.hexsha[:8])
            logging.info("Status for '{}': '{}' ({})".format(name, working_dir, branch_name))
            if not repo.head.is_detached:
                commits_ahead, commits_behind = self._count_commits(repo, branch_name)
                if commits_ahead and commits_behind:
                    print(colored("Your branch and 'origin/{}' have diverged,".format(branch_name), 'magenta'))
                    print(colored("and have {} and {} different commits each, respectively."
                                  .format(commits_ahead, commits_behind), 'magenta'))
                elif commits_ahead:
                    print(colored("Your branch is ahead of 'origin/{}' by {} commit."
                                  .format(branch_name, commits_ahead), 'magenta'))
                elif commits_behind:
                    print(colored("Your branch is behind 'origin/{}' by {} commit, and can be fast-forwarded."
                                  .format(branch_name, commits_behind), 'magenta'))
            clean = True
            indexed_files = repo.head.commit.diff()
            if len(indexed_files):
                print('Changes to be committed:')
                for indexed_file in indexed_files:
                    change_type = indexed_file.change_type[0]
                    print('\t{}'.format(change_type), colored(get_diff_path(indexed_file), 'green'))
                print()
                clean = False
            staged_files = repo.index.diff(None)
            if len(staged_files):
                print('Changes not staged for commit:')
                for staged_file in staged_files:
                    change_type = staged_file.change_type[0]
                    print('\t{}'.format(change_type), colored(get_diff_path(staged_file), 'red'))
                print()
                clean = False
            if len(repo.untracked_files):
                print('Untracked files:')
                for untracked_file in repo.untracked_files:
                    print(colored('\t{}'.format(untracked_file), 'red'))
                print()
                clean = False
            if clean:
                print('nothing to commit, working tree clean')
                print()

    def debug(self):
        """
        Remotely run program on target platform and attach debugger to it
        """
        pass

    def toolchain(self):
        """
        Prepare environment for LEDE toolchain

        The bash script is returned to the stdout which can be then evaluated in parent process to correctly set build
        environment for LEDE toolchain. It is then possible to use gcc and other tools from this SDK in external
        projects.
        """
        logging.info("Preparing toolchain environment...'")

        if self._use_glibc():
            target_name = 'target-arm_cortex-a9+neon_glibc-2.24_eabi'
            toolchain_name = 'toolchain-arm_cortex-a9+neon_gcc-5.4.0_glibc-2.24_eabi'
        else:
            target_name = 'target-arm_cortex-a9+neon_musl-1.1.16_eabi'
            toolchain_name = 'toolchain-arm_cortex-a9+neon_gcc-5.4.0_musl-1.1.16_eabi'

        staging_dir = os.path.join(self._working_dir, 'staging_dir')
        target_dir = os.path.join(staging_dir, target_name)
        toolchain_dir = os.path.join(staging_dir, toolchain_name)

        if not os.path.exists(target_dir):
            msg = "Target directory '{}' does not exist".format(target_dir)
            logging.error(msg)
            sys.stdout.write('echo {};\n'.format(msg))
            raise BuilderStop

        if not os.path.exists(toolchain_dir):
            msg = "Toolchain directory '{}' does not exist".format(toolchain_dir)
            logging.error(msg)
            sys.stdout.write('echo {};\n'.format(msg))
            raise BuilderStop

        env_path = os.environ.get('PATH', '')

        sys.stderr.write('# set environment with command:\n')
        sys.stderr.write('# eval $(./bb.py {} 2>/dev/null)\n'.format(' '.join(self._argv)))
        sys.stdout.write('TARGET="{}";\n'.format(target_dir))
        sys.stdout.write('TOOLCHAIN="{}";\n'.format(toolchain_dir))
        sys.stdout.write('export STAGING_DIR="${TARGET}";\n')

        if (toolchain_dir + '/bin') not in env_path:
            # export PATH only if it has not been exported already
            sys.stdout.write('export PATH="${TOOLCHAIN}/bin:$PATH";\n')

    def patch_whatsnew(self, path, version_short):
        """
        Patch 'whatsnew.md' header with firmware version

        Check if there is written some information before release.

        :param path:
            Path to whatsnew file.
        :param version_short:
            Short firmware version (without commit hash).
        """
        with open(path, 'r') as whatsnew:
            lines = whatsnew.readlines()

        if not lines:
            logging.error("File '{}' is empty".format(path))
            raise BuilderStop

        if not lines[0].startswith('## '):
            logging.error("Incorrect header in '{}' file: '{}'".format(path, lines[0].strip()))
            raise BuilderStop

        with open(path, 'w') as whatsnew:
            whatsnew.write('# {}\n\n'.format(version_short))
            whatsnew.writelines(lines)

    def patch_config_branches(self, config_original, config):
        """
        Patch original configuration with current branch hash

        The configuration tree is patched with the specific commit hash and then saved to default
        configuration file 'configs/default.yml'.

        :param config_original:
            Original configuration tree before changes.
        :param config:
            Configuration tree used for changes.
        """
        config_remote = config_original.remote
        config_aliases = config_remote.aliases

        def checkout_repo(repo, name, uri, branch):
            logging.info("Checkout repository '{}' to branch {}...".format(name, branch))
            self._checkout_repo(repo, RemoteWalker.Remote(name, uri, branch, True))

        def get_repo(name, location, project, branch):
            # expand server with original config formatter
            server = self._config.formatter(config_aliases[location])
            uri = '{}/{}'.format(server, project)
            repo_path = self._get_repo_path(name)
            if os.path.isdir(repo_path):
                repo = git.Repo(repo_path)
                if repo.remotes.origin.url == uri:
                    checkout_repo(repo, name, uri, branch)
                    return repo
                # directory contains different remote repository
                shutil.rmtree(repo_path, ignore_errors=True)
            logging.info("Cloning repository '{}'...".format(name))
            repo = git.Repo.clone_from(uri, repo_path, progress=RepoProgressPrinter())
            checkout_repo(repo, name, uri, branch)
            return repo

        del config.remote.branch

        default_location = config_remote.get('location', None)
        default_branch = config_remote.get('branch', 'master')

        # default location and branch could have special symbols
        default_location = self._config.formatter(default_location)
        default_branch = self._config.formatter(default_branch)

        for name, root_attributes in config_remote.repos.items():
            root_location = root_attributes.get('location', default_location)
            root_project = root_attributes.get('project', None)
            root_branch = root_attributes.get('branch', default_branch)
            match = root_attributes.get('match', None)
            if root_location and root_project:
                repo = get_repo(name, root_location, root_project, root_branch)
                commit_sha = repo.head.object.hexsha
                logging.debug("Set repository '{}' to commit {}...".format(name, commit_sha))
                config.remote.repos.get(name).branch = commit_sha
            if not match:
                continue
            for pattern, attributes in match.items():
                pattern_location = attributes.get('location', root_location)
                pattern_project = attributes.get('project', root_project)
                pattern_branch = attributes.get('branch', root_branch)
                repo = get_repo(name, pattern_location, pattern_project, pattern_branch)
                commit_sha = repo.head.object.hexsha
                logging.debug("Set repository '{}/{}' to commit {}...".format(name, pattern, commit_sha))
                config.remote.repos.get(name).match.get(pattern).branch = commit_sha

    def release(self, config_original, push=True):
        """
        Create release branch in git based on current configuration

        * check that all repositories are clean
        * modify default YAML configuration so that all repositories points to the specific commit
        * create new commit with modified configuration
        * tag new commit with firmware version and push it upstream

        :param config_original:
            Original configuration tree before changes.
        :param push:
            Push all changes to upstream.
        """
        repo_meta = git.Repo()

        if repo_meta.head.is_detached:
            logging.error("Meta repository is detached!")
            raise BuilderStop

        # save active branch to return back after creating release
        meta_active_branch = repo_meta.active_branch
        branch_name = meta_active_branch.name

        if repo_meta.is_dirty(untracked_files=True):
            logging.error("Meta repository is dirty!")
            raise BuilderStop

        for name, repo in self._repos.items():
            if repo and repo.is_dirty(untracked_files=True):
                logging.error("Repository '{}' is dirty!".format(name))
                raise BuilderStop

        # synchronise upstream repository with local one (fetch all tags)
        logging.debug("Fetching remote repository...")
        repo_meta.remotes.origin.fetch()

        commits_ahead, commits_behind = self._count_commits(repo_meta, branch_name)
        if commits_ahead or commits_behind:
            logging.error("Your branch and 'origin/{}' have diverged,".format(branch_name))
            raise BuilderStop

        # get short version for 'whatsnew.md' header
        fw_version_short = self.get_firmware_version(short=True, local_time=True)
        self.patch_whatsnew(self.WHATS_NEW, fw_version_short)

        # create commit with patched whatsnew file
        repo_meta.index.add([self.WHATS_NEW])
        repo_meta.index.commit(self.WHATS_NEW_COMMENT)

        logging.debug("Detaching head from branch...")
        repo_meta.head.reference = repo_meta.head.commit

        # copy configuration for modifications
        config = copy.deepcopy(config_original)

        # always checkout all repositories to correct commit
        config.remote.fetch_always = 'yes'

        logging.debug("Patching repository branches in config...")
        self.patch_config_branches(config_original, config)

        logging.info("Saving default configuration file to {}...".format(self.DEFAULT_CONFIG))
        with open(os.path.join(self.DEFAULT_CONFIG), 'w') as default_config:
            config.dump(default_config)

        logging.debug("Creating new release commit...")
        repo_meta.index.add([self.DEFAULT_CONFIG])
        repo_meta.index.commit("Release Firmware")

        fw_version_long = self.get_firmware_version()
        fw_version = '{}_{}'.format(self.FEED_FIRMWARE, fw_version_long)

        # check if full version has the same prefix as short one
        # it can happen when release is done just before midnight
        if not fw_version_long.startswith(fw_version_short):
            meta_active_branch.checkout()
            repo_meta.head.reset('HEAD~1')
            logging.error("Created wrong short version for '{}'".format(self.WHATS_NEW))
            logging.warning("Try to run release script again! This happens when release is done just before midnight")
            raise BuilderStop

        logging.info("Creating new release tag '{}'...".format(fw_version))
        repo_meta.create_tag(fw_version)
        if push:
            repo_meta.remotes.origin.push(fw_version)

        # return back to active branch
        meta_active_branch.checkout()
        if push:
            repo_meta.remotes.origin.push()

    def generate_key(self, secret_path, public_path):
        """
        Generate build kay pair compatible with LEDE build system

        :param secret_path:
            Path to secret key output file.
        :param public_path:
            Path to public key output file.
        """
        logging.info("Generating key pair...'")

        usign = self._get_utility(self.LEDE_USIGN)
        self._run(usign, '-G',
                  '-s', os.path.abspath(secret_path),
                  '-p', os.path.abspath(public_path),
                  '-c', 'Local build key')
