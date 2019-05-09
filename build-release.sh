#!/bin/bash
# Purpose: release script for braiins OS firmware

# The script:
# - runs a build of braiins-os for all specified targets
# - and generates scripts for packaging and signing the resulting build of
#
#
# Synopsis: ./build-release.sh KEYRINGSECRET RELEASE PACKAGEMERGE SUBTARGET1 [SUBTARGET2 [SUBTARGET3...]]
set -e
#
parallel_jobs=32
# default target is zynq
target=zynq
git_repo=git@gitlab.bo:x/braiins-os
feeds_url=https://feeds.braiins-os.org
pool_user=!non-existent-user!
fw_prefix=braiins-os
output_dir=output

key=`realpath $1`
shift
date_and_patch_level=$1
shift
package_merge=$1
shift
release_subtargets=$@

mtdparts="mtdparts=pl35x-nand:512k(boot),2560k(uboot),2m(fpga1),2m(fpga2),512k(uboot_env),512k(miner_cfg),22m(recovery),95m(firmware1),95m(firmware2)"
recovery_mtdparts_am1_s9="${mtdparts},144m@0x2000000(antminer_rootfs)"
recovery_mtdparts_dm1_g19="${mtdparts},512k@0x400000(dragonmint_env)"
recovery_mtdparts_dm1_g29="$recovery_mtdparts_dm1_g19"

#DRY_RUN=echo
STAGE1=y
CLONE=n

echo ID is: `id`
echo KEY is: $key
echo RELEASE_BUILD_DIR is: $RELEASE_BUILD_DIR
echo DATE and PATCH LEVEL: $date_and_patch_level
echo RELEASE SUBTARGETS: $release_subtargets

if [ $CLONE = y ]; then
    $DRY_RUN mkdir -p $RELEASE_BUILD_DIR
    $DRY_RUN cd $RELEASE_BUILD_DIR
    $DRY_RUN git clone $git_repo
    # Prepare build environment
    $DRY_RUN cd braiins-os
fi

. prepare-env.sh

function generate_sd_img() {
    subtarget=$1
    echo 'src_dir="'$2'"'
    echo 'sd_img="'$3'"'
    echo 'fw_img="'$4'"'

    echo 'sd_img_tmp=$(mktemp)'
    echo 'dd if=/dev/zero of=${sd_img_tmp} bs=1M count=46'

    echo 'sudo parted ${sd_img_tmp} --script mktable msdos'
    echo 'sudo parted ${sd_img_tmp} --script mkpart primary fat32 2048s 16M'
    echo 'sudo parted ${sd_img_tmp} --script mkpart primary ext4 16M 46M'

    echo 'loop=$(sudo kpartx -s -av ${sd_img_tmp} | sed '"'"'/^add map /s/.*\(loop[[:digit:]]\).*\+/\1/;q'"'"')'

    echo 'sudo mkfs.vfat /dev/mapper/${loop}p1'
    echo 'sudo mkfs.ext4 /dev/mapper/${loop}p2'

    echo 'sudo mount /dev/mapper/${loop}p1 /mnt'
    echo 'sudo cp ${src_dir}/sd/* /mnt/'
    recovery_mtdparts=$(eval echo \$recovery_mtdparts_${subtarget/-/_})
    [ -n "$recovery_mtdparts" ] && echo 'echo "recovery_mtdparts='"$recovery_mtdparts"'" | sudo tee -a /mnt/uEnv.txt'
    echo 'sudo umount /mnt'

    echo 'sudo mount /dev/mapper/${loop}p2 /mnt'
    echo 'sudo mkdir -p /mnt/work/work'
    echo 'sudo chmod 0 /mnt/work/work'
    echo 'sudo mkdir -p /mnt/upper/usr/share/upgrade'
    echo 'sudo cp ${fw_img} /mnt/upper/usr/share/upgrade/firmware.tar.bz2'
    echo 'sudo umount /mnt'

    echo 'sudo kpartx -d ${sd_img_tmp}'
    echo 'mv ${sd_img_tmp} ${sd_img}'
}

if [ "$date_and_patch_level" != "current" ]; then
	tag=`git tag | grep $date_and_patch_level | tail -1`
	if [ -z "$tag" ]; then
		echo "Error: supplied release \"$date_and_patch_level\" not found in tags"
		exit 4
	else
		$DRY_RUN git checkout $tag
	fi
fi

# get build version for current branch
version=$(./bb.py build-version)

# Iterate all releases/switch repo and build
for subtarget in $release_subtargets; do
    # latest release
    platform=$target-$subtarget
    # We need to ensure that feeds are update
    if [ $STAGE1 = y ]; then
	$DRY_RUN ./bb.py --platform $platform prepare
	$DRY_RUN ./bb.py --platform $platform prepare --update-feeds
	# build everything for a particular platform
	$DRY_RUN ./bb.py --platform $platform build --key $key -j$parallel_jobs -v
    fi

    package_name=${fw_prefix}_${subtarget}_${version}
    platform_dir=$output_dir/$package_name

    # Deploy SD and upgrade images
    for i in sd upgrade; do
	$DRY_RUN ./bb.py --platform $platform deploy local_$i:$platform_dir/$i --pool-user $pool_user
    done

    # Feeds deploy is specially handled as it has to merge with firmware packages
    packages_url=$feeds_url/$subtarget/Packages

    if [ "$package_merge" == "true" ]; then
	echo Merging package list with previous release for $platform...
	extra_feeds_opts="--feeds-base $packages_url"
    else
	echo Nothing has been published for $platform, skipping merge of Packages...
	extra_feeds_opts=
    fi
    $DRY_RUN ./bb.py --platform $platform deploy local_feeds:$platform_dir/feeds $extra_feeds_opts --pool-user $pool_user

    # Generate script for publication
    ($DRY_RUN cd $output_dir;
     pack_and_sign_script=pack-and-sign-$package_name.sh
     publish_dir=./publish/$package_name
     sd_img=$publish_dir/${fw_prefix}_${subtarget}_sd_${version}.img
     fw_img=$package_name/upgrade/${fw_prefix}_${subtarget}_ssh_${version}.tar.bz2
     gpg_opts="--armor --detach-sign --sign-with release@braiins.cz --sign"
     echo set -e > $pack_and_sign_script
     echo mkdir -p $publish_dir >> $pack_and_sign_script
     echo cp -r $package_name/feeds/ $publish_dir >> $pack_and_sign_script
     generate_sd_img $subtarget $package_name $sd_img $fw_img >> $pack_and_sign_script
     echo gpg2 $gpg_opts $sd_img >> $pack_and_sign_script
     echo for upgrade_img in $package_name/upgrade/\*\; do >> $pack_and_sign_script
     echo cp \$upgrade_img $publish_dir >> $pack_and_sign_script
     echo gpg2 $gpg_opts $publish_dir/\$\(basename \$upgrade_img\) >> $pack_and_sign_script
     echo
     echo done >> $pack_and_sign_script
    )
done
