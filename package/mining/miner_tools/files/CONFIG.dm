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

get_env_cfg() {
	fw_printenv -c "$env_config" -n "$1" 2>/dev/null
	return 0
}

# find DragonMint env MTD partition
env_mtd=$(cat /proc/mtd | sed -n '/dragonmint_env/s/\(mtd[[:digit:]]\+\).*/\1/p')
[ -n "$env_mtd" ] || return 0

env_config=$(mktemp)

cat > "$env_config" <<END
# MTD device name   Device offset   Env. size   Flash sector size
/dev/$env_mtd       0x00000         0x20000     0x20000
/dev/$env_mtd       0x00000         0x20000     0x20000
END

# get MAC from DragonMint environment
ETHADDR=$(get_env_cfg "ethaddr")

rm "$env_config"

[ -n "$ETHADDR" ] || return 0

# set that the target configuration is successful
CONFIG_RESULT="success"
