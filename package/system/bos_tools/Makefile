include $(TOPDIR)/rules.mk

PKG_NAME:=bos_tools
PKG_RELEASE:=1

PKG_MAINTAINER:=Libor Vasicek <libor.vasicek@braiins.cz>

include $(INCLUDE_DIR)/package.mk

define Package/bos_tools
  SECTION:=utils
  CATEGORY:=Utilities
  TITLE:=Utilities for managing braiins OS
endef

define Package/bos_tools/description
 Add bos script which is used for firmware management.
 It can do factory reset, boot to recovery mode, ...
endef

define Build/Compile
endef

define Package/bos_tools/install
	$(INSTALL_DIR) $(1)/usr/sbin
	$(INSTALL_BIN) ./files/bos $(1)/usr/sbin/
endef

$(eval $(call BuildPackage,bos_tools))
