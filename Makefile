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

# DESTDIR is staging only; run-installed finds its prefix relative to its bin directory.
install:
	install -d $(DESTDIR)$(PREFIX)/share/airloom
	tar --exclude='__pycache__' --exclude='*.pyc' -cf - airloom | tar -xf - -C $(DESTDIR)$(PREFIX)/share/airloom
	install -Dm755 run-installed $(DESTDIR)$(PREFIX)/bin/airloom
	install -Dm644 packaging/ai.stealthvision.Airloom.desktop $(DESTDIR)$(PREFIX)/share/applications/ai.stealthvision.Airloom.desktop
	install -Dm644 packaging/ai.stealthvision.Airloom.metainfo.xml $(DESTDIR)$(PREFIX)/share/metainfo/ai.stealthvision.Airloom.metainfo.xml
	install -Dm644 packaging/ai.stealthvision.Airloom.svg $(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/ai.stealthvision.Airloom.svg

uninstall:
	rm -f $(DESTDIR)$(PREFIX)/bin/airloom
	rm -rf $(DESTDIR)$(PREFIX)/share/airloom
	rm -f $(DESTDIR)$(PREFIX)/share/applications/ai.stealthvision.Airloom.desktop
	rm -f $(DESTDIR)$(PREFIX)/share/metainfo/ai.stealthvision.Airloom.metainfo.xml
	rm -f $(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/ai.stealthvision.Airloom.svg
