#!/usr/bin/env python

from os.path import exists
from setuptools import setup, find_packages

setup(
    name='celery-kubernetes',
    version='0.2.0',
    description='Native Kubernetes integration for Celery',
    url='https://github.com/comtravo/celery-kubernetes',
    keywords='celery,kubernetes',
    license='BSD',
    packages=find_packages(),
    long_description=(open('README.rst').read() if exists('README.rst') else ''),
    zip_safe=False,
    install_requires=list(open('requirements.txt').read().strip().split('\n')),
)
