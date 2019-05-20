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

env_config="/tmp/dragonmint_env.conf"

get_env_cfg() {
	fw_printenv -c "$env_config" -n "$1" 2>/dev/null
	return 0
}

dragonmint_cleanup() {
	# remove DragonMint configuration file because NAND will be overwritten by upgrade
	[ x"$DRY_RUN" != x"yes" ] && rm "$env_config"
}

# check if DragonMint configuration file is created
# the file is created during system pre-initialization phase
[ -f "$env_config" ] || return 0

# get MAC from DragonMint environment
ETHADDR=$(get_env_cfg "ethaddr")

dragonmint_cleanup

# check if NAND is not corrupted
[ -n "$ETHADDR" ] || return 0

# set that the target configuration is successful
CONFIG_RESULT="success"
