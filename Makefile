IMAGE ?= blast-radius-entry
PYTHON ?= python3
SOURCE ?= /tmp/rwx-sqlglot-base
KIT ?= $(CURDIR)/.cache/sample-kit
OUTPUT ?= $(CURDIR)/.cache/output.jsonl
SAMPLE := $(abspath $(KIT))/sample
KIT_URL := https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume "$(SAMPLE)/commits:/data/commits:ro" \
	--volume "$(SAMPLE)/tests.json:/data/tests.json:ro"

.PHONY: build sample sample-native kit index
build:
	docker build --platform linux/amd64 -t "$(IMAGE)" .

kit: $(KIT)/score.py

$(KIT)/score.py:
	mkdir -p "$(KIT)"
	curl --fail --location --retry 2 "$(KIT_URL)" -o "$(KIT)/kit.tar.gz"
	tar -xzf "$(KIT)/kit.tar.gz" -C "$(KIT)"

sample: build $(KIT)/score.py
	mkdir -p "$(dir $(OUTPUT))"
	docker run $(RUN_FLAGS) "$(IMAGE)" > "$(OUTPUT)"
	$(PYTHON) "$(KIT)/score.py" "$(SAMPLE)" "$(SAMPLE)/answers.json" "$(OUTPUT)"

sample-native: $(KIT)/score.py
	mkdir -p "$(dir $(OUTPUT))"
	$(PYTHON) selector.py --source "$(SOURCE)" --data "$(SAMPLE)" > "$(OUTPUT)"
	$(PYTHON) "$(KIT)/score.py" "$(SAMPLE)" "$(SAMPLE)/answers.json" "$(OUTPUT)"

index: $(KIT)/score.py
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) tools/build_index.py "$(SOURCE)" "$(SAMPLE)/tests.json" maps/coverage.json
