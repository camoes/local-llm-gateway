PY ?= python3

.PHONY: verify compile test live clean

verify: compile test status
	@echo "modelgate: gate en verde"

compile:
	$(PY) -m py_compile modelgate.py

test:
	$(PY) -m unittest discover -s tests -v

status:
	@if [ -f models.json ] && [ "$$SKIP_LIVE" != "1" ]; then \
		$(PY) modelgate.py status; \
	else \
		echo "modelgate: sin models.json o SKIP_LIVE=1 -> se omite la prueba en vivo"; \
	fi

live:
	$(PY) modelgate.py status --json

clean:
	rm -rf __pycache__ tests/__pycache__
