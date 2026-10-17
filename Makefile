# Match the effective user, including root when invoked through sudo or fakeroot.
ifeq ($(shell id -u),0)
prefix ?= /usr/local
sysconfdir ?= /etc
systemdsystemunitdir ?= $(prefix)/lib/systemd/system
unitdir = $(systemdsystemunitdir)
configfile = $(sysconfdir)/default/meo-ipv6-fix
service_template = systemd/meo-ipv6-fix.service.in
SYSTEMCTL_FLAGS ?=
else
prefix ?= $(HOME)/.local
sysconfdir ?= $(or $(XDG_CONFIG_HOME),$(HOME)/.config)
systemduserunitdir ?= $(sysconfdir)/systemd/user
unitdir = $(systemduserunitdir)
configfile = $(sysconfdir)/meo-ipv6-fix.env
service_template = systemd/meo-ipv6-fix-user.service.in
SYSTEMCTL_FLAGS ?= --user
endif

exec_prefix ?= $(prefix)
bindir ?= $(exec_prefix)/bin

UV ?= uv
INSTALL ?= install
SYSTEMCTL ?= systemctl

.DEFAULT_GOAL := check
.NOTPARALLEL:
.PHONY: sync test lint fmt build check install uninstall enable disable require-live

sync:
	$(UV) sync --locked

test:
	$(UV) run --locked python -X warn_default_encoding -W error::EncodingWarning -m unittest discover -s tests

lint:
	$(UV) run --locked ruff check .
	$(UV) run --locked ruff format --check .

fmt:
	$(UV) run --locked ruff format .

build:
	$(UV) build --no-sources --no-build-logs --clear

check: lint test

# Install files only. DESTDIR is a staging root and is never embedded in units.
# Live installations warn while the configuration lists no devices.
install: src/meo_ipv6_fix.py $(service_template) \
         systemd/meo-ipv6-fix.timer systemd/meo-ipv6-fix.env
	$(INSTALL) -D -m 755 src/meo_ipv6_fix.py "$(DESTDIR)$(bindir)/meo-ipv6-fix"
	@set -eu; \
		meo_service=$$(mktemp); \
		trap 'rm -f "$$meo_service"' EXIT; \
		sed -e 's|@BINDIR@|$(bindir)|g' -e 's|@CONFIG@|$(configfile)|g' "$(service_template)" > "$$meo_service"; \
		$(INSTALL) -D -m 644 "$$meo_service" "$(DESTDIR)$(unitdir)/meo-ipv6-fix.service"
	$(INSTALL) -D -m 644 systemd/meo-ipv6-fix.timer "$(DESTDIR)$(unitdir)/meo-ipv6-fix.timer"
	[ -e "$(DESTDIR)$(configfile)" ] || $(INSTALL) -D -m 600 systemd/meo-ipv6-fix.env "$(DESTDIR)$(configfile)"
	@if [ -z "$(DESTDIR)" ] && ! grep -E '^[[:space:]]*MEO_DEVICES[[:space:]]*=' "$(configfile)" \
		| tail -n 1 | grep -Eq '=.*[^[:space:]"'\'']'; then \
		printf 'warning: MEO_DEVICES is empty in %s; the service fails until it lists a device.\n' \
			"$(configfile)" >&2; \
	fi

# Stop the timer with `make disable` before removing a live installation.
uninstall:
	rm -fv "$(DESTDIR)$(bindir)/meo-ipv6-fix" \
		"$(DESTDIR)$(unitdir)/meo-ipv6-fix.service" \
		"$(DESTDIR)$(unitdir)/meo-ipv6-fix.timer"

require-live:
	@if [ -n "$(DESTDIR)" ]; then \
		printf '%s\n' 'Service activation requires an empty DESTDIR.' >&2; \
		exit 1; \
	fi

enable: require-live
	$(SYSTEMCTL) $(SYSTEMCTL_FLAGS) daemon-reload
	$(SYSTEMCTL) $(SYSTEMCTL_FLAGS) enable --now meo-ipv6-fix.timer

disable: require-live
	$(SYSTEMCTL) $(SYSTEMCTL_FLAGS) disable --now meo-ipv6-fix.timer
