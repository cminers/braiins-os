# Miner Firmware SD Card Version

This version of firmware is intended only for development and testing purposes.

## Prerequisites

Two things are needed for successful installation:

* Standard microSD card
* Hardware jumper

## SD Card Preparation

The SD card must be properly formatted and loaded with firmware and configuration files.

### Formating

Create one partition with the FAT16/32 filesystem. The size of partition is arbitrary.

The second partition with the Ext4 filesystem is optional and, when presented, it is
used for storing configuration files. If this partition is omitted then all miner
settings are discarded after a restart (i.e. the data are not persistent).

### Partition Content

The first partition should contain the following files:

* boot.bin
* fit.itb
* system.bit
* u-boot.img
* uEnv.txt (optional)

The second partition can be empty.

### Configuration

The file uEnv.txt should contain setup of a MAC address. The address is in following
format:

```
ethaddr=00:0A:35:DD:EE:FF
```

The last three numbers of MAC address determine the miner host name. For the address format example above, the host name would be:

```
miner-ddeeff
```

## Control Board Setup

The boot process of the control board can be controlled by two pairs of pins (J1, J2).

For SD boot, the following pin configurations are needed:

```
J1 - OFF
J2 - ON
```

This means that only J2 pins are connected. This can be done by jumper or any applicable
wire.
