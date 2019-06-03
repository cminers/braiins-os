include $(TOPDIR)/rules.mk

PKG_NAME:=bos_tools
PKG_RELEASE:=1

PKG_MAINTAINER:=Libor Vasicek <libor.vasicek@braiins.cz>

include $(INCLUDE_DIR)/package.mk

define Package/bos_tools
  SECTION:=utils
  CATEGORY:=Utilities
  TITLE:=Utilities for managing braiins OS
  DEPENDS:=+socat
endef

define Package/bos_tools/description
 Add bos script which is used for firmware management.
 It can do factory reset, boot to recovery mode, ...
endef

define Build/Compile
endef

define Package/bos_tools/conffiles
/etc/config/bos
endef

define Package/bos_tools/install
	$(INSTALL_DIR) $(1)/etc/
	$(CP) ./files/factory_cfg.config $(1)/etc
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) ./files/bos.config $(1)/etc/config/bos
	$(INSTALL_DIR) $(1)/usr/sbin
	$(INSTALL_BIN) ./files/bos $(1)/usr/sbin/
	$(INSTALL_BIN) ./files/run-background-process $(1)/usr/sbin/
endef

$(eval $(call BuildPackage,bos_tools))
