"""
Setup script for failnozzle.
"""
from setuptools import setup

if __name__ == '__main__':
    setup(name='failnozzle',
          version='1.0',
          packages=['failnozzle'],
          package_dir={'failnozzle': 'failnozzle'},
          install_requires=['gevent', 'Jinja2'],
          package_data={'failnozzle': ['*.txt']})
