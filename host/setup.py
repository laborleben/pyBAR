#!/usr/bin/env python

# Installing package from sources:
# python setup.py install
# For developers (creating a link to the sources):
# python setup.py develop
#
# Building source distribution:
# python setup.py sdist
# The generated source file is needed for installing by a tool like pip (pip install ...).


# from distutils.core import setup
from setuptools import setup
from setuptools import find_packages


f = open('VERSION', 'r')
version = f.readline().strip()
f.close()

author = 'Jens Janssen'
author_email = 'janssen@physik.uni-bonn.de'

setup(
    name='pyBAR',
    version=version,
    description='pyBAR: Bonn ATLAS Readout in Pyhton',
    url='https://silab-redmine.physik.uni-bonn.de/projects/pybar',
    license='BSD 3-Clause ("BSD New" or "BSD Simplified") License',
    long_description='',
    author=author,
    maintainer=author,
    author_email=author_email,
    maintainer_email=author_email,
    install_requires=['cython'],
    requires=['pySiLibUSB (>=1.0.0)', 'bitarray (>=0.8.1)', 'progressbar (>=2.4)', 'basil (>=2.0.0)'],
    packages=find_packages(),  # exclude=['*.tests', '*.test']),
    include_package_data=True,  # accept all data files and directories matched by MANIFEST.in or found in source control
    package_data={'': ['*.txt', 'VERSION'], 'docs': ['*'], 'examples': ['*'], 'pybar': ['*.yaml', '*.bit']},
)