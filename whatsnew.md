## All mining hardware types

- [feature] **temperature limits** are now configurable via configuration file. The two new configuration options are: ```--fan-temp-hot``` and ```--fan-temp-dangerous```. These options effectively override the temp limits in ```temp-def.h```
- [fix] **15m** and **24h hash rate** shows up only if the **full time period** of hash rate data has been collected
- [fix] enabled/disabled **indicator** for **Asic Boost** now works again in the overview status page
- [feature] **fan control** now has a new option ```--min-fans``` that specifies the minimum amount of fans that have to be operational (defaults to 1). Setting the option to "0" results in skipping the check for working fans.

## Antminer S9

- [feature] new configuration option allows **disabling temperature sensor scanning** (```--no-sensor-scan```). I2C scan log is now being stored into a separate file: ```/tmp/i2c_scan.log```
- [feature] support for **ADT7461 temperature sensor** that appears to be used on some hash board revisions

## Dragonmint T1

- [feature] support for **G29 revision** of the control board (no SD-card slot)
- [feature] web interface now allows configuring **full range of frequencies** (120 MHz - 1596 MHz) and **voltage levels** (1-31)

# 2019-01-24-0

## All mining hardware types

- [feature] bOS now automatically **detects availability** of a new version. The web UI now contains an indicator of new release availability (+ single click to install)
- [feature] **firmware upgrade process** is now more smooth when upgrading from **bOS** that is more than **2 releases old**
- [feature] miner status web page **no longer needs access to port 4028** of the miner, everything is provided via web proxy on the miner
- [feature] a new script **discover.py** scans the network range and provides information about **bOS devices** as well as **factory firmware devices**
- [feature] **fancontrol** completely rewritten, all mining hardware now uses the same **PID** controller algorithm. The automated fan control can be overriden and fan speed can be set manually
- [feature] it is now possible to run **upgrade2bos.py** with **--dry-run** parameter to create system backup and check if the firmware is likely succeed in transitioning to bOS
- [feature] **miner status page** is now the **default** section after login
- [feature] transition from factory firmware to bOS can now be supplied with a **post-upgrade script** that runs during the **first boot** of the machine running bOS for the first time. Official documentation provides more details.
- [feature] **macOS guide** for factory firmware transition added
- [feature] DHCP client now sends its **system hostname** to its DHCP server = there is a single source of truth with regards to the machine hostname

## Antminer S9

- [feature] upgrade to bOS is now possible for S9's running older firmware that has **4 NAND partitions**
- [feature] a multiplier allows changing **frequency** of either **per-chip calibration settings from the factory** or of user configured **per hash board base frequency**. Web interace adjusted accordingly. The functionality is also available through the API.
- [feature] it is now possible to restore the factory firmware **without having a backup** of the original firmware. The configuration is tailored from the running bOS and the restore2factory.py tool can be supplied with a factory firmware image downloaded from manufacturer's website.
- [feature] firmware now supports the **reset button** used for rebooting the machine. If the push button is held down for more than 5 seconds the machine is also "factory" reset and all bOS settings are erased (Note, that it doesn't switch back to original factory firmware)


# 2018-11-27-0

## Overview - major release; code name: Cobalt

A new major release that brings important features for S9's and unifies miner status page for all supported devices.

## Antminer S9

- [feature] per chip frequency tuning based on factory calibration constants
- [feature] alternative transitional firmware (```braiins-os_am1-s9_web_*tar.gz```) image that can be flashed via **factory
  web interface**
- [feature] support for S9j and R4 hardware types

## All mining hardware types

- [feature] new **miner overview** page with real-time telemetry data
- [feature] hash rate averaging exponential decay function in cgminer replaced with windowed average
- [feature] login text now indicates when running in recovery mode
- [feature] factory transition firmware preserves network settings of the original firmware. Optionally, user may request keeping the hostname, too (`upgrade2bos.py --keep-hostname`).
- [feature] bOS mode and version are now stored in `/etc/bos_{mode,version}`
- [feature] *upgrade2bos.py* can now skip NAND backup entirely and provide *configuration backup only*. The reason is to save space and speed up the process of upgrading big farms in bulk. The reverse script *restore2factory.py* can take the original factory image and combine it with the saved configuration. Thus, eliminating the need for full NAND backups completely.
- [feature] restore2factory now automatically detects when being run from SD card and acts accordingly
- [feature] new LED signalization scheme - see user documentation for details

### cgminer API improvements

- [feature] support for HTTP GET requests
- [feature] calculation of hourly hardware error rate
- [feature] Asic Boost status (yes/no)
- [feature] hashrate statistics for 1m, 15m, 24h using windowed average


# 2018-10-24-0

## Overview

*This release only contains images for Antminer S9.*

**Important:** If you wish to upgrade firmware (package `firmware`) via the web interface, it is neccesary to install package `libustream-openssl` first. This step is not required when upgrading via SSH.

## Antminer S9

- [feature] bmminer now supports overt **AsicBoost** via version rolling, latest bitstream from Bitmain
  has been integrated and [BIP310](https://github.com/bitcoin/bips/blob/master/bip-0310.mediawiki) support has been enabled. AsicBoost can be turned off in the interface.
- [feature] the transitional firmware now supports **flashing Antminers S9** in addition to originally supported S9i
- [feature] **per chain** frequency and **voltage control** now available in the interface
- [fix] Temperature reporting has been corrected to take measurements from
  the 'middle' sensor that is placed in the hot area of each
  hashboard. The displayed temperatures should better reflect the true
  temperature of the hashboard.

## All hardware types

- [fix] package list update triggered via web UI doesn't report missing SSL support error anymore
- [fix] opkg no longer reports errors about missing feeds due to an attempt to fetch
- [fix] Firmware reports its real release version during stratum subscribe. The default cgminer version has been removed.
