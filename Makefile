BOB_VERSION := f735c12192bf95684e6ae1ae27c400b8170fc6d8
BOB_STAGE := stable
VERSION_NUMBER_STR := $(shell grep '^version =' game.project | awk -F '=' '{print $$2}' | awk '{$$1=$$1};1')
VERSION_NUMBER := $(shell echo $(VERSION_NUMBER_STR))
LIVEUPDATE_CLIENT_VERSION ?=
BOB_FILE := /tmp/bob.$(BOB_VERSION).jar

$(BOB_FILE):
	@echo "Download bob.jar"
	@wget 'http://d.defold.com/archive/$(BOB_STAGE)/$(BOB_VERSION)/bob/bob.jar' -O $(BOB_FILE)

buildliveupdatehighres: $(BOB_FILE)
	@rm -rf liveupdate_dist
	@rm -rf build
	@echo "Build liveupdate highres"
	@rm -rf "liveupdate_dist"
	@java -jar $(BOB_FILE) \
		--settings game.project \
		--settings game_high_res.project \
		--texture-compression true \
		--bundle-output dist \
		--build-report-html build/report.html \
		--variant debug \
		--archive \
		--platform wasm-web \
		--use-async-build-server \
		--liveupdate yes \
		resolve build bundle
	@echo "Build liveupdate"
	@rm -rf "dist/output/$(VERSION_NUMBER)/liveupdatehighres"
	@unzip -o -q "liveupdate_dist/*.zip" -d "liveupdate_dist/"
	@python3 "tools/liveupdate_pack.py" $(if $(strip $(LIVEUPDATE_CLIENT_VERSION)),--client-version "$(LIVEUPDATE_CLIENT_VERSION)")
	@mkdir -p "dist/output/$(VERSION_NUMBER)"
	@mv "liveupdate_zip/" "dist/output/$(VERSION_NUMBER)/liveupdatehighres/"
	@rm -rf "liveupdate_zip"
	@rm -rf "liveupdate_dist"

buildliveupdatelowres: $(BOB_FILE)
	@rm -rf liveupdate_dist
	@echo "Build liveupdate lowres"
	@rm -rf "liveupdate_dist"
	@java -jar $(BOB_FILE) \
		--settings game.project \
		--settings game_low_res.project \
		--texture-compression true \
		--bundle-output dist \
		--build-report-html build/report.html \
		--variant debug \
		--archive \
		--platform wasm-web \
		--use-async-build-server \
		--liveupdate yes \
		resolve build bundle
	@echo "Build liveupdate"
	@rm -rf "dist/output/$(VERSION_NUMBER)/liveupdatelowres"
	@unzip -o -q "liveupdate_dist/*.zip" -d "liveupdate_dist/"
	@python3 "tools/liveupdate_pack.py" --restore_from_tree $(if $(strip $(LIVEUPDATE_CLIENT_VERSION)),--client-version "$(LIVEUPDATE_CLIENT_VERSION)")
	@mkdir -p "dist/output/$(VERSION_NUMBER)"
	@mv "liveupdate_zip" "dist/output/$(VERSION_NUMBER)/liveupdatelowres"
	@rm -rf "liveupdate_zip"
	@rm -rf "liveupdate_dist"

buildliveupdateres:
	@echo "Build liveupdate resources"
	@rm -rf liveupdate_dist
	@rm -rf liveupdate_zip
	@rm -rf dist/output
	@mkdir -p dist/output
	@rm -rf "dist/output/$(VERSION_NUMBER)/"
	@mkdir -p "dist/output/$(VERSION_NUMBER)/"
	@make buildliveupdatehighres
	@make buildliveupdatelowres
	@make report

buildweb: $(BOB_FILE)
	@java -jar $(BOB_FILE) \
		--settings game.project \
		--texture-compression true \
		--bundle-output dist \
		--build-report-html build/report.html \
		--variant debug \
		--archive \
		--platform wasm-web \
		--use-async-build-server \
		--liveupdate yes \
		resolve build bundle
	@echo 'Result: "dist/defold_live_unbundler/index.html"'
	@echo "Report: build/report.html"


copyliveupdateres:
	@cp -r "dist/output/$(VERSION_NUMBER)/." "dist/defold_live_unbundler/"

buildlocalweb:
	@make buildweb
	@make buildliveupdateres
	@make copyliveupdateres


report:
	@echo "Build liveupdate collections report"
	@python3 "tools/liveupdate_collections_report.py" --version-dir "dist/output/$(VERSION_NUMBER)"
	@echo "Report: dist/output/$(VERSION_NUMBER)/liveupdate_collections_report.html"

serve3:
	@echo "Serve dist directory on http://localhost:8000"
	@cd dist/defold_live_unbundler && python3 -m http.server 8000

install_requirements:
	@pip3 install -r tools/requirements.txt

format:
	stylua --config-path ".stylua.toml" --glob "**/*.lua" --glob "**/*.script" --glob "**/*.gui_script" .
