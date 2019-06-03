include $(TOPDIR)/rules.mk
include $(INCLUDE_DIR)/kernel.mk

PKG_NAME:=miner_tools
PKG_RELEASE:=1

PKG_MAINTAINER:=Libor Vasicek <libor.vasicek@braiins.cz>

include $(INCLUDE_DIR)/package.mk

define Package/miner_tools
  SECTION:=utils
  CATEGORY:=Utilities
  TITLE:=Utilities for managing miner
  DEPENDS:=+bos_tools
endef

define Package/miner_tools/description
 Add miner script which is used for miner namagement.
 It can do factory reset, boot to recovery mode or turn on miner's LEDs.
endef

define Build/Compile
endef

ifeq ($(SUBTARGET),am1-s9)
define Package/miner_tools/subtarget_install
	$(CP) ./files/CONFIG.am $(1)/usr/share/upgrade/CONFIG
endef
endif

ifneq (,$(filter dm1-g9 dm1-g19 dm1-g29,$(SUBTARGET)))
define Package/miner_tools/subtarget_install
	$(CP) ./files/CONFIG.dm $(1)/usr/share/upgrade/CONFIG
endef
endif

define Package/miner_tools/install
	$(INSTALL_DIR) $(1)/usr/sbin
	$(INSTALL_BIN) ./files/miner $(1)/usr/sbin/
	$(INSTALL_DIR) $(1)/usr/share/upgrade
	$(CP) ./files/CONTROL $(1)/usr/share/upgrade/CONTROL.bos
	$(call Package/miner_tools/subtarget_install,$(1))
endef

$(eval $(call BuildPackage,miner_tools))
