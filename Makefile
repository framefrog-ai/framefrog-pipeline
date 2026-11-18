.PHONY: demo test api clean

demo:
	python demo.py

quick:
	python demo.py --quick

test:
	python -m pytest -q

api:
	uvicorn framefrog.api:app --host 0.0.0.0 --port 8080 --reload

clean:
	rm -rf outputs assets/sample
