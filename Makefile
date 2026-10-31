PREFIX ?= /usr/local
DESTDIR ?=
PYTHON ?= python3

.PHONY: run test check check-js flatpak install uninstall

run:
	$(PYTHON) -m airloom

test:
	$(PYTHON) -m unittest discover -s tests -v

check: test check-js
	$(PYTHON) -m compileall -q airloom

check-js:
	for file in airloom/resources/*.js; do node --check "$$file" || exit; done
	node --test tests/js/*.test.js

flatpak:
	./scripts/build-flatpak.sh

# Destinations are single-quoted for the shell (embedded quotes become '\''),
# so a staging path with spaces, globs, or quotes stays one literal argument.
shell_quote = '$(subst ','\'',$(1))'
dest = $(call shell_quote,$(DESTDIR)$(PREFIX))

# DESTDIR is staging only; run-installed finds its prefix relative to its bin directory.
install:
	install -d -- $(dest)/share/airloom
	tar --exclude='__pycache__' --exclude='*.pyc' -cf - airloom | tar -xf - -C $(dest)/share/airloom
	install -Dm755 -- run-installed $(dest)/bin/airloom
	install -Dm644 -- packaging/ai.stealthvision.Airloom.desktop $(dest)/share/applications/ai.stealthvision.Airloom.desktop
	install -Dm644 -- packaging/ai.stealthvision.Airloom.metainfo.xml $(dest)/share/metainfo/ai.stealthvision.Airloom.metainfo.xml
	install -Dm644 -- packaging/ai.stealthvision.Airloom.svg $(dest)/share/icons/hicolor/scalable/apps/ai.stealthvision.Airloom.svg

uninstall:
	rm -f -- $(dest)/bin/airloom
	rm -rf -- $(dest)/share/airloom
	rm -f -- $(dest)/share/applications/ai.stealthvision.Airloom.desktop
	rm -f -- $(dest)/share/metainfo/ai.stealthvision.Airloom.metainfo.xml
	rm -f -- $(dest)/share/icons/hicolor/scalable/apps/ai.stealthvision.Airloom.svg
