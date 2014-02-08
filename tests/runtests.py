#!/usr/bin/env python2

"""Pitivi tests runner."""

import os
import sys
import unittest


parent = os.path.abspath(os.path.join(os.getcwd(), os.pardir))
sys.path.append(os.path.join(parent, "pitivi/coptimizations/.libs"))

from pitivi.check import initialize_modules
initialize_modules()


def gettestnames(file_names):
    test_names = [file_name[:-3] for file_name in file_names]
    return test_names

loader = unittest.TestLoader()

# Set verbosity.
descriptions = 1
verbosity = 1
if 'VERBOSE' in os.environ:
    descriptions = 2
    verbosity = 2
from pitivi.utils import loggable as log
log.init('PITIVI_DEBUG', 1)

# Make available to configure.py the top level dir.
dir = os.path.dirname(os.path.abspath(__file__))
top_srcdir = os.path.split(dir)[0]
os.environ.setdefault('PITIVI_TOP_LEVEL_DIR', top_srcdir)

# Pick which tests to run.
TEST_CASE = os.getenv("TESTCASE")
if TEST_CASE:
    test_names = [TEST_CASE]
else:
    test_names = gettestnames(sys.argv[1:])
suite = loader.loadTestsFromNames(test_names)
if not list(suite):
    raise Exception("No tests found")

# Run the tests.
testRunner = unittest.TextTestRunner(descriptions=descriptions,
    verbosity=verbosity)
result = testRunner.run(suite)
if result.failures or result.errors:
    sys.exit(1)
