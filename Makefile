all: env

check: pep8 lint test

clean:
	rm -rf env

env:
	virtualenv --python=python2.7 --no-site-packages env
	. env/bin/activate && pip install -r dependencies.txt

lint:
	. env/bin/activate && pylint --rcfile=pylintrc failnozzle

pep8:
	. env/bin/activate && pep8 failnozzle

test:
	. env/bin/activate && nosetests failnozzle

run:
	. env/bin/activate && python -m failnozzle.server

